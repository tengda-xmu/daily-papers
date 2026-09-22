from __future__ import annotations

from datetime import datetime
from typing import Protocol

from src.models import RawRecord, SourceStatus


class SourceAdapter(Protocol):
    name: str

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]: ...

    @property
    def status(self) -> SourceStatus: ...

