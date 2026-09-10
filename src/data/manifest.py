"""Simple JSON fetch manifest for resume / dedup / incremental updates.

Deliberately not a database. One JSON file, one entry per
(provider, key, start, end) chunk that was attempted. `key` is whatever
the fetcher finds natural to disambiguate chunks: a symbol+year for
Massive, a country+currency+month for MQL5/Forex Factory, a series id
for FRED, etc.

Status values: "complete", "empty", "failed".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class ManifestEntry:
    provider: str
    key: str
    start: str
    end: str
    status: str  # "complete" | "empty" | "failed"
    rows: int = 0
    retrieved_at: str = field(default_factory=utcnow_iso)
    checksum: Optional[str] = None
    path: Optional[str] = None
    error: Optional[str] = None

    def composite_key(self) -> str:
        return f"{self.provider}:{self.key}:{self.start}:{self.end}"


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self._entries: Dict[str, ManifestEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for item in raw.get("entries", []):
            entry = ManifestEntry(**item)
            self._entries[entry.composite_key()] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utcnow_iso(),
            "entries": [asdict(e) for e in self._entries.values()],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".manifest_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)

    def is_complete(self, provider: str, key: str, start: str, end: str) -> bool:
        entry = self._entries.get(f"{provider}:{key}:{start}:{end}")
        return entry is not None and entry.status in ("complete", "empty")

    def get(
        self, provider: str, key: str, start: str, end: str
    ) -> Optional[ManifestEntry]:
        return self._entries.get(f"{provider}:{key}:{start}:{end}")

    def record(self, entry: ManifestEntry) -> None:
        self._entries[entry.composite_key()] = entry
        self.save()

    def entries_for(
        self, provider: str, key: Optional[str] = None
    ) -> List[ManifestEntry]:
        out = []
        for e in self._entries.values():
            if e.provider != provider:
                continue
            if key is not None and e.key != key:
                continue
            out.append(e)
        return out

    def latest_complete_end(
        self, provider: str, key: Optional[str] = None
    ) -> Optional[dt.date]:
        """Latest `end` date among complete/empty entries, used to compute the
        delta range for incremental updates."""
        candidates = [
            dt.date.fromisoformat(e.end)
            for e in self.entries_for(provider, key)
            if e.status in ("complete", "empty")
        ]
        return max(candidates) if candidates else None

    def failed_entries(self, provider: Optional[str] = None) -> List[ManifestEntry]:
        return [
            e
            for e in self._entries.values()
            if e.status == "failed" and (provider is None or e.provider == provider)
        ]
