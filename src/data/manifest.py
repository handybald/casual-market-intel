"""JSON fetch manifest: resume / dedup / interval-aware incremental updates.

Deliberately not a database. One JSON file, one entry per
(provider, key, start, end) chunk that was attempted. `key` is whatever
the fetcher finds natural to disambiguate chunks -- and MUST include
every axis that changes the meaning of the stored data (e.g. Massive
keys are "{symbol}:{timeframe}:{adjustment}", not just the symbol, so a
1min/raw checkpoint can never be silently reused as a 5min/adjusted one).

Status values and what they mean for resume/watermark logic:
  "complete"    finalized, verified coverage for [start, end]. Counts
                toward the completion watermark and is skipped on rerun.
  "empty"       finalized, verified ABSENCE of data for [start, end]
                (e.g. a market holiday, or a FRED window before a series
                started). Also counts toward the watermark.
  "provisional" successfully fetched, but not yet safe to treat as
                final (covers "today", an open trading session, an
                in-progress calendar month, or a FRED window inside the
                revision-overlap horizon). NEVER counts toward the
                watermark and is ALWAYS re-attempted on the next run.
  "failed"      the attempt errored or failed validation. Never counts
                toward the watermark; retried on the next run.

The watermark (`completion_watermark`) is the latest date such that
[dataset_start, watermark] is covered by CONTIGUOUS, verified
("complete"/"empty") entries with no gap and no failure in between --
NOT simply the max `end` across all entries. A failure or provisional
entry in the middle of the range holds the watermark back until it is
repaired, so a later success further out never causes the gap to be
silently skipped.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERIFIED_STATUSES = ("complete", "empty")
RETRYABLE_STATUSES = ("failed", "provisional")


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
    start: str  # ISO date, inclusive
    end: str  # ISO date, inclusive
    status: str  # "complete" | "provisional" | "empty" | "failed"
    rows: int = 0
    retrieved_at: str = field(default_factory=utcnow_iso)
    # Set only when an entry's checksum is refreshed by a sibling-
    # reconciliation pass (see fetch/massive.py
    # `_reverify_and_refresh_siblings`) WITHOUT the entry's own range
    # being refetched -- distinct from `retrieved_at`, which always
    # stays the original acquisition time for that data. None if this
    # entry has never been through such a reconciliation.
    verified_at: Optional[str] = None
    checksum: Optional[str] = None
    path: Optional[str] = None
    error: Optional[str] = None
    # Opaque, provider-specific audit metadata (e.g. {"timeframe": "1min",
    # "adjustment": "raw"}). Never interpreted by Manifest itself.
    request_meta: Dict[str, Any] = field(default_factory=dict)

    def composite_key(self) -> str:
        return f"{self.provider}:{self.key}:{self.start}:{self.end}"

    def start_date(self) -> dt.date:
        return dt.date.fromisoformat(self.start)

    def end_date(self) -> dt.date:
        return dt.date.fromisoformat(self.end)


@dataclass(frozen=True)
class CoverageGap:
    start: dt.date
    end: dt.date
    reason: str  # "provisional" | "future" | "failed" | "missing"

    @property
    def is_failure(self) -> bool:
        """True for gaps that represent a real problem (failed or never
        attempted); false for gaps that are expected/benign (provisional,
        future)."""
        return self.reason in ("failed", "missing")


class ArtifactIntegrityError(RuntimeError):
    """Raised when a manifest entry claims a status the on-disk artifact
    does not actually support (missing file, checksum mismatch)."""


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
            item.setdefault("request_meta", {})
            item.setdefault("verified_at", None)
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

    # -- artifact integrity ------------------------------------------------
    def verify_artifact(self, entry: ManifestEntry) -> bool:
        """True if the artifact an entry claims to back still exists and
        (when a checksum was recorded) still matches it. An entry with no
        `path` (e.g. an early MQL5 failure record) is considered to have
        nothing to verify and passes trivially."""
        if entry.path is None:
            return True
        p = Path(entry.path)
        if not p.exists():
            return False
        if entry.checksum is None:
            return True
        try:
            return checksum_file(p) == entry.checksum
        except OSError:
            return False

    def invalidate_entries_for_missing_or_corrupt_path(
        self, provider: str, key: str, path: Path
    ) -> List[ManifestEntry]:
        """Any verified (complete/empty) entry for (provider, key) backed
        by `path` where the artifact is now missing or fails checksum
        verification is no longer trustworthy: mark it "failed" so it is
        re-fetched/re-verified.

        This must run BEFORE rebuilding a shared artifact (e.g. Massive's
        one-parquet-per-year file backing many month checkpoints). The
        original bug: after deleting the file and only rebuilding one of
        several months that shared it, `update_checksum_for_path` would
        silently refresh EVERY sibling entry's checksum to match the
        rebuilt file -- "blessing" checkpoints whose actual rows were no
        longer present anywhere. Calling this first, before any write,
        ensures every sibling is explicitly invalidated (not silently
        re-trusted) the moment the shared artifact is known to be gone or
        corrupt; a later reconciliation pass can then re-verify each
        sibling's specific coverage against the rebuilt content before
        ever refreshing its checksum again (see fetch/massive.py).

        Returns the list of entries that were invalidated (empty if the
        artifact is intact or no entries reference it).
        """
        path_str = str(path)
        candidates = [e for e in self._entries.values() if e.path == path_str and e.status in VERIFIED_STATUSES]
        if not candidates:
            return []
        if path.exists():
            # Artifact is still present -- only invalidate entries whose
            # OWN recorded checksum no longer matches it (corruption),
            # not every entry indiscriminately.
            invalidated = [e for e in candidates if not self.verify_artifact(e)]
        else:
            invalidated = candidates

        for e in invalidated:
            self.record(
                ManifestEntry(
                    provider=e.provider,
                    key=e.key,
                    start=e.start,
                    end=e.end,
                    status="failed",
                    error="backing artifact missing/corrupt; invalidated pending re-verification",
                    request_meta=e.request_meta,
                )
            )
        return invalidated

    def update_checksum_for_path(self, path: Path, new_checksum: str) -> None:
        """A shared artifact (e.g. one year's Massive parquet backing many
        month-entries) was rewritten. Every entry pointing at it must have
        its checksum refreshed, or their integrity checks would spuriously
        fail (or worse, silently pass a stale hash) on the next run."""
        path_str = str(path)
        changed = False
        for e in self._entries.values():
            if e.path == path_str and e.checksum != new_checksum:
                e.checksum = new_checksum
                changed = True
        if changed:
            self.save()

    # -- lookups -------------------------------------------------------
    def is_complete(self, provider: str, key: str, start: str, end: str) -> bool:
        """True if this exact [start, end] chunk is durably, verifiably
        done and can be skipped. Provisional entries are NEVER considered
        complete -- they are always retried."""
        entry = self._entries.get(f"{provider}:{key}:{start}:{end}")
        if entry is None or entry.status not in VERIFIED_STATUSES:
            return False
        return self.verify_artifact(entry)

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
        return sorted(out, key=lambda e: (e.start, e.end))

    # -- interval-aware coverage ------------------------------------------
    def _verified_intervals(
        self, provider: str, key: Optional[str]
    ) -> List[Tuple[dt.date, dt.date]]:
        intervals = []
        for e in self.entries_for(provider, key):
            if e.status in VERIFIED_STATUSES and self.verify_artifact(e):
                intervals.append((e.start_date(), e.end_date()))
        intervals.sort()
        return intervals

    def completion_watermark(
        self, provider: str, key: Optional[str], dataset_start: dt.date
    ) -> Optional[dt.date]:
        """Latest date such that [dataset_start, watermark] is verified,
        contiguous coverage. Returns None if dataset_start itself isn't
        covered yet. A gap, a provisional entry, or a failed entry
        anywhere before the requested point stops the watermark dead --
        it does NOT skip ahead to a later successful chunk."""
        intervals = self._verified_intervals(provider, key)
        watermark: Optional[dt.date] = None
        cursor = dataset_start
        for start, end in intervals:
            if start > cursor:
                break  # gap
            if end >= cursor:
                watermark = end
                cursor = end + dt.timedelta(days=1)
        return watermark

    def coverage_gaps(
        self,
        provider: str,
        key: Optional[str],
        dataset_start: dt.date,
        dataset_end: dt.date,
    ) -> List[Tuple[dt.date, dt.date]]:
        """Sub-ranges of [dataset_start, dataset_end] NOT covered by a
        verified interval -- i.e. what update_data.py must still (re)fetch,
        including any failed/provisional stretch in the middle of
        otherwise-complete history."""
        intervals = self._verified_intervals(provider, key)
        gaps: List[Tuple[dt.date, dt.date]] = []
        cursor = dataset_start
        for start, end in intervals:
            if end < dataset_start or start > dataset_end:
                continue
            clipped_start = max(start, dataset_start)
            clipped_end = min(end, dataset_end)
            if clipped_start > cursor:
                gaps.append((cursor, clipped_start - dt.timedelta(days=1)))
            cursor = max(cursor, clipped_end + dt.timedelta(days=1))
            if cursor > dataset_end:
                break
        if cursor <= dataset_end:
            gaps.append((cursor, dataset_end))
        return gaps

    def classify_gaps(
        self,
        provider: str,
        key: Optional[str],
        dataset_start: dt.date,
        dataset_end: dt.date,
        today: Optional[dt.date] = None,
    ) -> List[CoverageGap]:
        """Like `coverage_gaps`, but says WHY each unverified sub-range
        isn't finalized -- callers (e.g. update_data.py) need to tell
        "successfully collected, intentionally still provisional" apart
        from "genuinely missing or failed", since a `coverage_gaps()`
        result alone conflates the two (a provisional entry never counts
        as "verified", so it always shows up as a gap even on a run that
        did exactly what it should).

        reason (decided per-day by the MOST RECENT -- by `retrieved_at`
        -- overlapping entry, not just "any overlapping entry has status
        X"; see `_classify_gap_range`):
          "provisional" -- the newest evidence for this date is a
                            "provisional" entry with an intact backing
                            artifact (successfully fetched, pending
                            finalization). Not an error.
          "future"      -- nothing overlaps this date and it is after
                            `today` -- nothing could have been fetched
                            yet. Not an error.
          "failed"      -- the newest evidence for this date is a
                            "failed" entry, OR a "provisional" entry
                            whose backing artifact is missing/corrupt.
          "missing"     -- no entry at all overlaps this date (never
                            attempted).

        A later, wider (or narrower) successful attempt for some of a
        gap's dates supersedes an older overlapping "failed" record for
        those same dates -- the classic bug this guards against: Sept
        1-10 fails, Sept 1-11 is later fetched successfully as
        provisional, but the stale Sept 1-10 failure must not keep the
        whole (now-repaired) range reported as "failed". A gap is split
        into separate CoverageGap segments wherever the winning
        classification changes across it, so an only-partially-repaired
        failure correctly keeps its still-untouched portion "failed"
        while the repaired portion becomes "provisional"/is dropped.
        """
        gaps = self.coverage_gaps(provider, key, dataset_start, dataset_end)
        entries = self.entries_for(provider, key)
        result: List[CoverageGap] = []
        for gap_start, gap_end in gaps:
            overlapping = [e for e in entries if not (e.end_date() < gap_start or e.start_date() > gap_end)]
            result.extend(self._classify_gap_range(gap_start, gap_end, overlapping, today))
        return result

    def _classify_gap_range(
        self,
        gap_start: dt.date,
        gap_end: dt.date,
        overlapping: List[ManifestEntry],
        today: Optional[dt.date],
    ) -> List[CoverageGap]:
        day_reasons: List[str] = []
        cursor = gap_start
        while cursor <= gap_end:
            covering = [e for e in overlapping if e.start_date() <= cursor <= e.end_date()]
            if not covering:
                day_reasons.append("future" if (today is not None and cursor > today) else "missing")
            else:
                newest = max(covering, key=lambda e: dt.datetime.fromisoformat(e.retrieved_at))
                if newest.status == "provisional":
                    day_reasons.append("provisional" if self.verify_artifact(newest) else "failed")
                elif newest.status == "failed":
                    day_reasons.append("failed")
                elif newest.status in VERIFIED_STATUSES:
                    # A "complete"/"empty" entry covering a date that is
                    # still inside an unverified gap can only mean its
                    # own artifact failed verification (otherwise
                    # `coverage_gaps` would already have excluded this
                    # date entirely) -- not safe to call anything but a
                    # failure.
                    day_reasons.append("failed")
                else:
                    day_reasons.append("missing")
            cursor += dt.timedelta(days=1)

        result: List[CoverageGap] = []
        start_idx = 0
        for i in range(1, len(day_reasons) + 1):
            if i == len(day_reasons) or day_reasons[i] != day_reasons[start_idx]:
                seg_start = gap_start + dt.timedelta(days=start_idx)
                seg_end = gap_start + dt.timedelta(days=i - 1)
                result.append(CoverageGap(seg_start, seg_end, day_reasons[start_idx]))
                start_idx = i
        return result

    def failed_entries(
        self, provider: Optional[str] = None, key: Optional[str] = None
    ) -> List[ManifestEntry]:
        return [
            e
            for e in self._entries.values()
            if e.status == "failed"
            and (provider is None or e.provider == provider)
            and (key is None or e.key == key)
        ]

    def provisional_entries(
        self, provider: Optional[str] = None, key: Optional[str] = None
    ) -> List[ManifestEntry]:
        return [
            e
            for e in self._entries.values()
            if e.status == "provisional"
            and (provider is None or e.provider == provider)
            and (key is None or e.key == key)
        ]
