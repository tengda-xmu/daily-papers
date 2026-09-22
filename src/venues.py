"""Journal groups used to keep CNS coverage visible in retrieval and ranking."""
from __future__ import annotations

import re


CNS_CORE_JOURNALS = ("Nature", "Science", "Cell")
CNS_SUBJOURNALS = (
    "Nature Communications",
    "Nature Machine Intelligence",
    "Nature Computational Science",
    "Nature Biomedical Engineering",
    "Nature Electronics",
    "Nature Materials",
    "npj Computational Materials",
    "npj Artificial Intelligence",
    "Communications Engineering",
    "Science Advances",
    "Science Robotics",
    "Cell Reports",
    "Cell Systems",
    "Cell Reports Physical Science",
    "iScience",
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def classify_venue(value: str) -> str:
    venue = _clean(value)
    if not venue:
        return ""
    # Check subjournals first: names such as Science Advances and Nature
    # Communications begin with a CNS flagship title.
    for journal in CNS_SUBJOURNALS:
        if _clean(journal) in venue:
            return "CNS 子刊"
    for journal in CNS_CORE_JOURNALS:
        target = _clean(journal)
        if venue == target or venue.startswith(target + " ") or venue.startswith(target + "("):
            return "CNS 正刊"
    return ""


def venue_priority(value: str) -> float:
    group = classify_venue(value)
    return {"CNS 正刊": 1.0, "CNS 子刊": 0.8}.get(group, 0.0)


def cns_query() -> str:
    journals = CNS_CORE_JOURNALS + CNS_SUBJOURNALS
    return "(" + " OR ".join(f'SRCTITLE("{journal}")' for journal in journals) + ")"
