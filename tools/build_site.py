"""Build the static daily digest and readable archives for GitHub Pages."""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.catalog import (JOURNALS, SOURCE_CATALOG, STATE_LABELS, TOPIC_CATALOG,
                         VENUE_GROUPS, paper_facets, source_state, string_list)

DATA = ROOT / "data"
OUT = ROOT / "site"
TEMPLATES = ROOT / "tools" / "templates"
ASSETS = ROOT / "tools" / "assets"
CHINA = timezone(timedelta(hours=8))
REPO_URL = "https://github.com/tengda-xmu/daily-papers"
MANUAL_UPDATE_URL = f"{REPO_URL}/actions/workflows/daily.yml"
SOURCE_LABELS = {item["id"]: item["label"] for item in SOURCE_CATALOG}
SETUP_HINTS = {
    "Elsevier": "尚未配置 Elsevier 检索授权。",
    "Google Scholar": "尚未配置 Google Scholar 检索服务。",
    "ResearchGate": "尚未导入本地连接器导出的论文。",
    "微信公众号": "尚未添加公众号订阅地址。",
}
AUTH_HINTS = {
    "Web of Science": "Clarivate Web of Science 需要机构 API 授权。",
}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def safe_url(value, fallback="#") -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return esc(value)
    except ValueError:
        pass
    return fallback


def display_time(value) -> tuple[str, str]:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        stamp = stamp.astimezone(CHINA)
        return stamp.strftime("%Y-%m-%d"), stamp.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "日期待更新", "尚未生成"


def template(name: str, **values) -> str:
    return Template((TEMPLATES / name).read_text(encoding="utf-8")).substitute(values)


def document(content: str, *, title: str, root: str = "./", active: str = "daily") -> str:
    version = hashlib.sha256(
        (ASSETS / "site.css").read_bytes() + (ASSETS / "site.js").read_bytes()
    ).hexdigest()[:10]
    topic_links = "".join(
        f'<a href="{root}?topic={esc(key)}#reading">{esc(item["label"])}</a>'
        for key, item in TOPIC_CATALOG.items()
    )
    return template(
        "page.html", content=content.lstrip(), title=esc(title), root=root, version=version,
        topic_links=topic_links, repo=REPO_URL,
        daily_current='aria-current="page"' if active == "daily" else "",
        archive_current='aria-current="page"' if active == "archive" else "",
    )


def paper_card(paper: dict, tier: str, rank: int = 0) -> str:
    facets = paper_facets(paper)
    title = paper.get("title") or "未命名论文"
    doi = paper.get("doi") or ""
    url = safe_url(paper.get("landing_url") or paper.get("url") or
                   (f"https://doi.org/{doi}" if doi else ""))
    source = paper.get("source") or "来源待补充"
    venue = paper.get("venue") or ""
    authors = ", ".join(string_list(paper.get("authors")))
    topics = string_list(paper.get("topic_tags"))
    tags = "".join(
        f'<span class="tag">{esc(TOPIC_CATALOG.get(topic, {}).get("label", topic))}</span>'
        for topic in topics
    )
    summary = paper.get("summary") or paper.get("abstract") or "暂无摘要，请查看原文。"
    recommendation = paper.get("recommendation") or ""
    deep = paper.get("deep_read") or {}
    deep_items = [(label, deep.get(key)) for key, label in (
        ("problem", "研究问题"), ("method", "研究方法"), ("findings", "主要发现"),
        ("limitations", "研究局限"), ("connection", "方向关联"),
    ) if deep.get(key)]
    deep_html = ""
    if tier == "core" and deep_items:
        deep_html = '<details class="deep-read"><summary>精读要点</summary><dl>' + "".join(
            f'<div><dt>{label}</dt><dd>{esc(value)}</dd></div>' for label, value in deep_items
        ) + '</dl></details>'
    citation = ". ".join(value for value in (authors, title, venue,
                                            str(paper.get("published_at") or "")[:4],
                                            f"https://doi.org/{doi}" if doi else "") if value)
    actions = f'<a href="{url}" target="_blank" rel="noopener noreferrer">阅读原文<span class="sr-only">（新窗口）</span></a>' if url != "#" else ""
    if paper.get("oa_url"):
        oa_url = safe_url(paper["oa_url"])
        if oa_url != "#":
            actions += f'<a href="{oa_url}" target="_blank" rel="noopener noreferrer">开放获取</a>'
    if doi:
        actions += f'<a href="https://doi.org/{esc(doi)}" target="_blank" rel="noopener noreferrer">DOI</a>'
    actions += f'<button type="button" class="text-button copy-citation" data-citation="{esc(citation)}">复制引用</button>'
    title_html = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{esc(title)}</a>' if url != "#" else esc(title)
    search = " ".join(str(value or "") for value in
                      (title, authors, venue, summary, paper.get("abstract"), doi))
    venue_line = f'<p class="venue">{esc(venue)} <span class="venue-group">{esc(facets["venue_group"])}</span></p>' if venue else ""
    recommendation_html = f'<p class="recommendation"><strong>推荐理由</strong>{esc(recommendation)}</p>' if recommendation else ""
    return f'''
<article class="paper" data-sources="{esc(json.dumps(facets['source_ids'], ensure_ascii=False))}"
 data-topics="{esc(json.dumps(topics, ensure_ascii=False))}" data-venue="{esc(facets['venue_group'])}"
 data-journal="{esc(facets['journal'])}" data-search="{esc(search)}" data-rank="{rank}"
 data-date="{esc(paper.get('published_at'))}">
  <div class="paper-meta"><span class="source">{esc(SOURCE_LABELS.get(source, source))}</span><span>{esc(str(paper.get('published_at') or '')[:10])}</span></div>
  <h3>{title_html}</h3>
  <p class="authors">{esc(authors)}</p>{venue_line}
  <p class="abstract">{esc(summary)}</p>{recommendation_html}
  <div class="tags">{tags}</div>{deep_html}
  <div class="paper-actions">{actions}</div>
</article>'''


def source_directory(statuses: dict, counts: Counter) -> str:
    sections = []
    for kind, label, description in (
        ("adapter", "数据采集", "已实现的四类来源及本轮采集状态。"),
        ("planned", "待接入来源", "已纳入目录，尚未启用自动采集。"),
        ("platform", "出版平台", "依据论文的期刊与原文链接归类，可筛选本期已有记录。"),
    ):
        rows = []
        for item in (s for s in SOURCE_CATALOG if s["kind"] == kind):
            state, message = source_state(item, statuses)
            if state == "configuration_missing":
                message = SETUP_HINTS.get(item["id"], "该来源需要完成初始配置。")
            elif state == "authorization_required":
                message = AUTH_HINTS.get(item["id"], "该来源需要机构授权。")
            elif kind == "adapter":
                message = {
                    "ok": "本轮采集完成。", "no_data": "本轮检索未返回论文。",
                    "not_run": "尚无本轮采集记录。", "error": "采集失败，请查看运行记录。",
                    "access_denied": "授权或访问受限，请检查来源设置。",
                    "quota_exhausted": "检索额度已用尽，等待恢复或调整配额。",
                }.get(state, "请查看运行记录了解详情。")
            state_class = "ok" if state == "ok" else "pending" if state in ("planned", "platform", "not_run") else "warn"
            rows.append(f'''
<div class="source-row">
  <div><a class="source-name" href="{safe_url(item['url'])}" target="_blank" rel="noopener noreferrer">{esc(item['label'])}</a><p>{esc(message)}</p></div>
  <div class="source-state"><span class="state {state_class}">{esc(STATE_LABELS.get(state, '状态待确认'))}</span>
  <button type="button" class="text-button" data-source-filter="{esc(item['id'])}">本期 {counts[item['id']]} 篇</button></div>
</div>''')
        if not rows:
            continue
        sections.append(f'<details class="directory-group" {"open" if kind == "adapter" else ""}><summary>{label}<span>{len(rows)} 项</span></summary><p class="directory-description">{description}</p>{"".join(rows)}</details>')
    return "".join(sections)


def journal_directory() -> str:
    groups = []
    for group in VENUE_GROUPS:
        journals = [j for j in JOURNALS if j["group"] == group["id"]]
        if not journals:
            continue
        buttons = "".join(
            f'<button type="button" class="journal-button" data-journal-filter="{esc(j["name"])}" data-group="{esc(group["id"])}">{esc(j["name"])}</button>'
            for j in journals
        )
        groups.append(f'<details class="directory-group" {"open" if group["id"].startswith("CNS") else ""}><summary>{esc(group["id"])}<span>{len(journals)} 本</span></summary><div class="journal-list">{buttons}</div></details>')
    return "".join(groups)


def render(payload: dict, *, archive_date: str | None = None) -> str:
    core = payload.get("core") or []
    extended = payload.get("extended") or []
    all_papers = core + extended
    statuses = payload.get("source_status") or {}
    facets_list = [paper_facets(p) for p in all_papers]
    counts = Counter(source for f in facets_list for source in f["source_ids"])
    source_options = []
    for kind, label in (("adapter", "数据采集"), ("planned", "待接入来源"), ("platform", "出版平台")):
        options = []
        for item in (s for s in SOURCE_CATALOG if s["kind"] == kind):
            state, _ = source_state(item, statuses)
            options.append(f'<option value="{esc(item["id"])}">{esc(item["label"])} · {counts[item["id"]]} 篇 · {esc(STATE_LABELS.get(state, state))}</option>')
        if options:
            source_options.append(f'<optgroup label="{label}">{"".join(options)}</optgroup>')
    unknown_sources = sorted(set(counts) - set(SOURCE_LABELS))
    source_options.extend(f'<option value="{esc(s)}">{esc(s)} · {counts[s]} 篇</option>' for s in unknown_sources)
    topics = list(dict.fromkeys([*TOPIC_CATALOG, *(t for p in all_papers for t in string_list(p.get("topic_tags")))]))
    topic_options = "".join(f'<option value="{esc(t)}">{esc(TOPIC_CATALOG.get(t, {}).get("label", t))}</option>' for t in topics)
    group_options = "".join(f'<option value="{esc(g["id"])}">{esc(g["id"])}</option>' for g in VENUE_GROUPS)
    journals = {j["name"]: j["group"] for j in JOURNALS}
    journals.update({f["journal"]: f["venue_group"] for f in facets_list if f["journal"]})
    journal_options = "".join(f'<option value="{esc(name)}" data-group="{esc(group)}">{esc(name)}</option>' for name, group in journals.items())
    day, generated = display_time(payload.get("generated_at"))
    issue = archive_date or day
    root = "../" if archive_date else "./"
    core_html = "".join(paper_card(p, "core", i) for i, p in enumerate(core))
    extended_html = "".join(paper_card(p, "extended", i) for i, p in enumerate(extended))
    missing = sum(source_state(s, statuses)[0] == "configuration_missing" for s in SOURCE_CATALOG if s["kind"] == "adapter")
    ok = sum(source_state(s, statuses)[0] == "ok" for s in SOURCE_CATALOG if s["kind"] == "adapter")
    adapter_count = sum(s["kind"] == "adapter" for s in SOURCE_CATALOG)
    notice = ""
    if missing:
        notice = f'<div class="notice"><span class="status-dot" aria-hidden="true"></span><p>{missing} 类数据来源尚未完成配置，完成后可生成对应推荐。</p><a href="#sources">查看接入状态</a></div>'
    elif not all_papers:
        notice = '<div class="notice"><span class="status-dot" aria-hidden="true"></span><p>本期尚无符合条件的论文，请查看来源状态或历史归档。</p><a href="#sources">查看来源状态</a></div>'
    cns_children = sum(j["group"] == "CNS 子刊" for j in JOURNALS)
    archive_notice = f'<p class="archive-notice">正在阅读 {esc(archive_date)} 归档。<a href="../">返回最新一期</a></p>' if archive_date else ""
    content = template(
        "daily.html", title="每日论文推荐", day=esc(issue), generated=esc(generated),
        archive_notice=archive_notice, history_url="./" if archive_date else "archive/", manual_update_url=MANUAL_UPDATE_URL,
        notice=notice, source_options="".join(source_options),
        topic_options=topic_options, group_options=group_options, journal_options=journal_options,
        core_count=len(core), extended_count=len(extended), total=len(all_papers),
        core_html=core_html, extended_html=extended_html,
        core_empty="hidden" if core else "", extended_empty="hidden" if extended else "",
        no_data="" if not all_papers else "hidden", no_match="hidden",
        sources=source_directory(statuses, counts), journals=journal_directory(),
        source_count=len(SOURCE_CATALOG), journal_count=len(JOURNALS), group_count=len(VENUE_GROUPS),
        cns_children=cns_children, adapter_count=adapter_count, ok_count=ok, root=root, repo=REPO_URL,
    )
    return document(content, title=f"{issue} · 每日论文推荐" if archive_date else "每日论文推荐 | 腾达", root=root,
                    active="archive" if archive_date else "daily")


def build_archive() -> None:
    archive_out = OUT / "archive"
    archive_out.mkdir(parents=True, exist_ok=True)
    entries = []
    for source in sorted((DATA / "archive").glob("*.json"), reverse=True):
        payload = read_json(source, {})
        shutil.copyfile(source, archive_out / source.name)
        (archive_out / f"{source.stem}.html").write_text(render(payload, archive_date=source.stem), encoding="utf-8")
        core, extended = len(payload.get("core") or []), len(payload.get("extended") or [])
        entries.append(f'''<li class="archive-entry"><div><a class="archive-date" href="{esc(source.stem)}.html">{esc(source.stem)}</a><p>核心推荐 {core} 篇 · 扩展阅读 {extended} 篇</p></div><a class="text-link" href="{esc(source.stem)}.json" download>下载数据</a></li>''')
    content = template("archive.html", entries="".join(entries) or '<li class="empty">尚无历史归档，首期日报生成后会在此展示。</li>', count=len(entries))
    (archive_out / "index.html").write_text(document(content, title="历史归档 | 每日论文推荐", root="../", active="archive"), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSETS, OUT / "assets", dirs_exist_ok=True)
    payload = read_json(DATA / "daily.json", {})
    (OUT / "index.html").write_text(render(payload), encoding="utf-8")
    (OUT / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    build_archive()


if __name__ == "__main__":
    main()
