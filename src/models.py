"""Data contracts used by the paper sources and publishing pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Mapping


@dataclass
class RawRecord:
    """A source-independent paper/article record."""

    source: str
    source_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    abstract: str = ""
    published_at: str = ""
    doi: str = ""
    landing_url: str = ""
    oa_url: str = ""
    citation_count: int | None = None
    source_score: float = 0.0
    topic_tags: list[str] = field(default_factory=list)
    summary: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source = str(self.source or "unknown").strip()
        self.source_id = str(self.source_id or "").strip()
        self.title = " ".join(str(self.title or "").split())
        self.authors = _string_list(self.authors)
        self.venue = str(self.venue or "").strip()
        self.abstract = " ".join(str(self.abstract or "").split())
        self.published_at = str(self.published_at or "").strip()
        self.doi = normalize_doi(self.doi)
        self.landing_url = str(self.landing_url or "").strip()
        self.oa_url = str(self.oa_url or "").strip()
        if self.citation_count is not None:
            try:
                self.citation_count = int(self.citation_count)
            except (TypeError, ValueError):
                self.citation_count = None
        self.source_score = float(self.source_score or 0.0)
        self.topic_tags = _string_list(self.topic_tags)
        self.summary = " ".join(str(self.summary or "").split())
        self.raw_metadata = dict(self.raw_metadata or {})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any], source: str = "") -> "RawRecord":
        return cls(
            source=source or item.get("source", "unknown"),
            source_id=item.get("source_id", item.get("id", item.get("url", ""))),
            title=item.get("title", item.get("name", "")),
            authors=item.get("authors", item.get("author", [])),
            venue=item.get("venue", item.get("journal", item.get("publication", ""))),
            abstract=item.get("abstract", item.get("description", item.get("summary", ""))),
            published_at=item.get("published_at", item.get("published", item.get("pubDate", item.get("year", "")))),
            doi=item.get("doi", ""),
            landing_url=item.get("landing_url", item.get("link", item.get("url", ""))),
            oa_url=item.get("oa_url", item.get("pdf_url", "")),
            citation_count=item.get("citation_count", item.get("citations")),
            source_score=item.get("source_score", 0.0),
            topic_tags=item.get("topic_tags", []),
            summary=item.get("summary", ""),
            raw_metadata=dict(item),
        )


@dataclass
class SourceStatus:
    source: str
    status: str
    count: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace(";", ",").replace(" and ", ",").split(",")
    return [str(x).strip() for x in value if str(x).strip()]


def normalize_doi(value: Any) -> str:
    doi = str(value or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    return doi.rstrip(" .;,").lower()


def parse_date(value: Any) -> datetime | None:
    """Parse ISO, RSS and year-only dates into timezone-aware datetimes."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT",
                "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y"):
        try:
            parsed = datetime.strptime(text[:30], fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def in_date_window(value: Any, since: datetime, until: datetime) -> bool:
    parsed = parse_date(value)
    if parsed is None:
        return True
    start = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    end = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}", text):
        return start.year <= int(text) <= end.year
    return start <= parsed <= end
