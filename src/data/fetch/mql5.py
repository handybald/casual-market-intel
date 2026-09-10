"""MQL5 Economic Calendar ingestion.

IMPORTANT LIMITATION: the MQL5 Calendar API (CalendarValueHistory) is
only callable from code running inside the MetaTrader 5 terminal --
there is no HTTP endpoint. `mql5_exporter/EconomicCalendarExporter.mq5`
is the piece that must be run manually inside MetaTrader; it produces
a CSV under MQL5\\Files\\ that the user copies (or symlinks) to
`data/raw/mql5/us_macro_calendar.csv`.

This module does NOT talk to MetaTrader. It:
  1. locates that CSV,
  2. parses it,
  3. checks which requested months are actually covered,
  4. records manifest entries per covered month (so downstream
     normalize/validate steps and `update_data.py` know what's usable),
  5. reports any requested months that are missing so the user knows
     exactly what to re-export.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from ..config import AppConfig
from ..dates import iter_date_chunks
from ..manifest import Manifest, ManifestEntry, checksum_file, utcnow_iso

logger = logging.getLogger(__name__)


class Mql5Row(NamedTuple):
    event_id: str
    event_name: str
    country_code: str
    currency_code: str
    importance: str
    event_time_raw: str  # as written by the exporter, e.g. "2020.01.15 13:30:00"
    unit: str
    actual_value: Optional[float]
    forecast_value: Optional[float]
    prev_value: Optional[float]
    revised_prev_value: Optional[float]
    source_timezone: str


def _parse_float(raw: str) -> Optional[float]:
    raw = raw.strip()
    return float(raw) if raw else None


def parse_mql5_csv(path: Path) -> List[Mql5Row]:
    rows: List[Mql5Row] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(
                Mql5Row(
                    event_id=r["event_id"],
                    event_name=r["event_name"],
                    country_code=r["country_code"],
                    currency_code=r["currency_code"],
                    importance=r["importance"],
                    event_time_raw=r["event_time"],
                    unit=r.get("unit", ""),
                    actual_value=_parse_float(r["actual_value"]),
                    forecast_value=_parse_float(r["forecast_value"]),
                    prev_value=_parse_float(r["prev_value"]),
                    revised_prev_value=_parse_float(r["revised_prev_value"]),
                    source_timezone=r.get("source_timezone", "UNKNOWN") or "UNKNOWN",
                )
            )
    return rows


def _row_date(row: Mql5Row) -> dt.date:
    # exporter writes "YYYY.MM.DD HH:MM:SS" via MQL5 TimeToString
    return dt.datetime.strptime(row.event_time_raw, "%Y.%m.%d %H:%M:%S").date()


class Mql5CoverageReport(NamedTuple):
    covered_months: List[str]
    missing_months: List[str]
    total_rows: int
    csv_path: Path


def ingest_mql5_calendar(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
) -> Mql5CoverageReport:
    """Ingest whatever the MQL5 exporter has already produced and report
    coverage against [start_date, end_date]. Does not fetch anything itself.
    """
    provider_cfg = config.provider("mql5")
    csv_path = config.resolve_path(provider_cfg["input_csv"])

    requested_chunks = list(iter_date_chunks(start_date, end_date, "month"))
    manifest_key = f"{config.macro_country}:{config.macro_currency}"

    if not csv_path.exists():
        logger.error(
            "[MQL5] input CSV not found at %s -- run mql5_exporter/"
            "EconomicCalendarExporter.mq5 inside MetaTrader first",
            csv_path,
        )
        for chunk in requested_chunks:
            manifest.record(
                ManifestEntry(
                    provider="mql5",
                    key=manifest_key,
                    start=chunk.start.isoformat(),
                    end=chunk.end.isoformat(),
                    status="failed",
                    error="MQL5 exporter CSV not found; run .mq5 script in MetaTrader",
                )
            )
        return Mql5CoverageReport([], [c.label for c in requested_chunks], 0, csv_path)

    rows = parse_mql5_csv(csv_path)
    file_checksum = checksum_file(csv_path)

    rows_by_month: Dict[str, int] = {}
    for row in rows:
        try:
            d = _row_date(row)
        except ValueError:
            logger.warning("[MQL5] unparseable event_time %r, skipping", row.event_time_raw)
            continue
        month_key = f"{d:%Y-%m}"
        rows_by_month[month_key] = rows_by_month.get(month_key, 0) + 1

    covered_months = []
    missing_months = []
    for chunk in requested_chunks:
        count = rows_by_month.get(chunk.label, 0)
        if count > 0:
            covered_months.append(chunk.label)
            manifest.record(
                ManifestEntry(
                    provider="mql5",
                    key=manifest_key,
                    start=chunk.start.isoformat(),
                    end=chunk.end.isoformat(),
                    status="complete",
                    rows=count,
                    checksum=file_checksum,
                    path=str(csv_path),
                )
            )
            logger.info("[MQL5] %s: %d rows (from exporter CSV)", chunk.label, count)
        else:
            missing_months.append(chunk.label)
            manifest.record(
                ManifestEntry(
                    provider="mql5",
                    key=manifest_key,
                    start=chunk.start.isoformat(),
                    end=chunk.end.isoformat(),
                    status="failed",
                    error="not present in exporter CSV",
                )
            )

    if missing_months:
        logger.warning(
            "[MQL5] %d month(s) missing from %s: %s",
            len(missing_months),
            csv_path,
            ", ".join(missing_months),
        )

    return Mql5CoverageReport(covered_months, missing_months, len(rows), csv_path)
