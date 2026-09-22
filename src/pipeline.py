"""Fetch, normalize, rank and publish the daily paper recommendations."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.models import RawRecord, SourceStatus, parse_date
from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter


TOPICS = {
    "ai_maintenance": (
        "large language model", "llm", "agent", "foundation model", "rag",
        "generative ai", "predictive maintenance", "prognostics", "fault diagnosis",
        "condition monitoring", "remaining useful life", "digital twin",
        "故障诊断", "预测维护",
    ),
    "generative_design": (
        "generative design", "topology optimization", "surrogate model",
        "structural optimization", "reliability-based design", "reliability optimization",
        "uncertainty quantification", "physics-informed", "生成式结构", "拓扑优化",
        "代理模型", "可靠性优化",
    ),
    "fatigue_reliability": (
        "structural fatigue", "fatigue life", "fatigue crack", "fracture mechanics",
        "damage tolerance", "probabilistic fatigue", "structural reliability",
        "bayesian reliability", "fatigue", "结构疲劳", "疲劳寿命", "断裂力学", "可靠性",
    ),
}


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(title or "").casefold())


def _identity(record: RawRecord) -> tuple[str, str]:
    if record.doi:
        return "doi", record.doi
    normalized = normalize_title(record.title)
    if normalized:
        return "title", normalized
    return "source_id", f"{record.source}:{record.source_id}".casefold()


def _quality(record: RawRecord) -> tuple[int, int, int, float]:
    return (
        bool(record.abstract) * len(record.abstract),
        bool(record.landing_url) + bool(record.oa_url),
        bool(record.authors) + bool(record.venue),
        record.source_score,
    )


def deduplicate(records: Iterable[RawRecord]) -> list[RawRecord]:
    """Collapse DOI/title duplicates while retaining the richest metadata."""
    merged: dict[tuple[str, str], RawRecord] = {}
    order: list[tuple[str, str]] = []
    for item in records:
        if not isinstance(item, RawRecord) or not item.title.strip():
            continue
        key = _identity(item)
        if key not in merged:
            merged[key] = item
            order.append(key)
            continue
        current = merged[key]
        best, other = (item, current) if _quality(item) > _quality(current) else (current, item)
        for field in ("authors", "venue", "abstract", "published_at", "doi",
                      "landing_url", "oa_url", "citation_count"):
            if not getattr(best, field) and getattr(other, field):
                setattr(best, field, getattr(other, field))
        best.raw_metadata.update(other.raw_metadata)
        best.source_score = max(best.source_score, other.source_score)
        sources = set(best.raw_metadata.get("sources", [best.source]))
        sources.add(other.source)
        best.raw_metadata["sources"] = sorted(sources)
        merged[key] = best
    return [merged[key] for key in order]


def classify(record: RawRecord) -> RawRecord:
    text = f"{record.title} {record.abstract} {record.venue}".casefold()
    record.topic_tags = [
        name for name, terms in TOPICS.items()
        if any(term.casefold() in text for term in terms)
    ]
    return record


def _sort_key(record: RawRecord) -> tuple:
    published = parse_date(record.published_at)
    return (
        len(record.topic_tags),
        record.source_score,
        record.citation_count or 0,
        published or datetime.min.replace(tzinfo=timezone.utc),
        normalize_title(record.title),
    )


def rank(records: Iterable[RawRecord]) -> list[RawRecord]:
    return sorted((classify(record) for record in deduplicate(records)),
                  key=_sort_key, reverse=True)


def fallback_summary(record: RawRecord) -> dict[str, object]:
    abstract = re.sub(r"\s+", " ", record.abstract or "").strip()
    if abstract:
        summary = abstract[:600].rstrip()
        if len(abstract) > 600:
            summary += "\u2026"
        method = "\u57fa\u4e8e\u539f\u6587\u6458\u8981\u7684\u89c4\u5219\u6458\u8981"
    else:
        summary = f"\u672c\u6587\u805a\u7126\u300a{record.title}\u300b\uff0c\u6682\u672a\u63d0\u4f9b\u53ef\u7528\u6458\u8981\uff0c\u8bf7\u67e5\u770b\u539f\u6587\u3002"
        method = "\u539f\u6587\u672a\u63d0\u4f9b\u6458\u8981\uff0c\u57fa\u4e8e\u6807\u9898\u751f\u6210"
    return {
        "summary": summary,
        "method": method,
        "recommendation": "\u5efa\u8bae\u7ed3\u5408\u539f\u6587\u6838\u67e5\u7814\u7a76\u65b9\u6cd5\u3001\u6570\u636e\u96c6\u548c\u5b9e\u9a8c\u7ed3\u679c\u3002",
        "deep_read": {
            "problem": summary,
            "method": method,
            "findings": "\u9700\u8981\u9605\u8bfb\u539f\u6587\u7ed3\u679c\u90e8\u5206\u786e\u8ba4\u3002",
            "limitations": "\u9700\u8981\u9605\u8bfb\u539f\u6587\u8ba8\u8bba\u90e8\u5206\u786e\u8ba4\u3002",
            "connection": "\u4e0e\u7814\u7a76\u65b9\u5411\u7684\u5173\u8054\u7531\u5173\u952e\u8bcd\u5339\u914d\u5f97\u5230\u3002",
        },
    }


def _llm_summary(record: RawRecord) -> dict[str, object] | None:
    """Ask an OpenAI-compatible endpoint for a structured Chinese digest."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    prompt = (
        "Using only the metadata and abstract below, return JSON in Chinese with fields "
        "summary, method, recommendation, problem, findings, limitations, connection. "
        "Do not invent results. If evidence is missing, write that the original paper must be checked.\n\n"
        f"Title: {record.title}\nAuthors: {", ".join(record.authors)}\n"
        f"Venue: {record.venue}\nAbstract: {record.abstract[:7000]}"
    )
    body = json.dumps({
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    request = Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
        required = ("summary", "method", "recommendation", "problem", "findings", "limitations", "connection")
        if all(result.get(key) for key in required):
            return {key: str(result[key]).strip() for key in required} | {"llm_model": model}
    except (HTTPError, URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def summarize(record: RawRecord, allow_llm: bool = True) -> dict[str, object]:
    return (_llm_summary(record) if allow_llm else None) or fallback_summary(record)


def record_id(record: RawRecord) -> str:
    value = record.doi or normalize_title(record.title) or record.source_id
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def build_adapters() -> list:
    queries = [x.strip() for x in os.getenv("ELSEVIER_QUERIES", "").split("||") if x.strip()]
    return [
        ElsevierAdapter(queries=queries or None),
        GoogleScholarAdapter(),
        ResearchGateImportAdapter(),
        WeChatRSSAdapter(),
    ]


def _read_setting(name: str, default: int) -> int:
    if os.getenv(name):
        try:
            return max(0, int(os.environ[name]))
        except ValueError:
            pass
    configured = Path("config/sources.yml")
    if configured.exists():
        key = name[4:].lower()
        for line in configured.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}:"):
                try:
                    return max(0, int(line.split(":", 1)[1].strip()))
                except ValueError:
                    break
    return default


def run_pipeline(
    since: datetime | None = None,
    until: datetime | None = None,
    adapters: Sequence | None = None,
    output_path: str | Path | None = None,
) -> dict:
    until = until or datetime.now(timezone.utc)
    since = since or (until - timedelta(days=2))
    all_records: list[RawRecord] = []
    statuses: dict[str, dict] = {}
    for adapter in adapters or build_adapters():
        try:
            all_records.extend(adapter.fetch(since, until))
            status = adapter.status
        except Exception as exc:
            status = SourceStatus(getattr(adapter, "name", "unknown"), "error", message=str(exc))
        statuses[status.source] = status.to_dict()

    ranked = rank(all_records)
    max_core = _read_setting("MAX_CORE", 5)
    max_extended = _read_setting("MAX_EXTENDED", 5)
    papers: list[dict] = []
    for index, record in enumerate(ranked):
        data = record.to_dict()
        data["id"] = record_id(record)
        data.update(summarize(record, allow_llm=index < max_core))
        papers.append(data)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "core": papers[:max_core],
        "extended": papers[max_core:max_core + max_extended],
        "papers": papers,
        "source_status": statuses,
    }
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_text(encoded, encoding="utf-8")
        # Shanghai uses a fixed UTC+08:00 offset, so archive naming should not
        # depend on the optional system timezone database (tzdata).
        shanghai = timezone(timedelta(hours=8))
        archive_date = until.astimezone(shanghai).date().isoformat()
        archive = path.parent / "archive" / f"{archive_date}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(encoded, encoding="utf-8")
    return payload


def main() -> None:
    run_pipeline(output_path=Path("data/daily.json"))


if __name__ == "__main__":
    main()
