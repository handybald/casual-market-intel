"""Shared helpers for writing/reading normalized MacroEvent lists in the interim layer."""
from __future__ import annotations

from pathlib import Path
from typing import List

import pandas as pd

from ..schemas import MacroEvent

EVENT_COLUMNS = list(MacroEvent.model_fields.keys())
_DATETIME_FIELDS = {"release_timestamp_utc", "release_timestamp_ny", "source_timestamp", "retrieval_timestamp_utc"}
_DATE_FIELDS = {"official_vintage_date"}


def events_to_dataframe(events: List[MacroEvent]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    return pd.DataFrame([e.model_dump() for e in events])


def merge_write_events(events: List[MacroEvent], path: Path) -> pd.DataFrame:
    """Merge new events into an existing interim parquet file, deduping by
    event_id (new rows win), and write atomically."""
    new_df = events_to_dataframe(events)
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    if not combined.empty:
        combined = combined.drop_duplicates(subset=["event_id"], keep="last")
        combined = combined.sort_values("release_timestamp_utc").reset_index(drop=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp_path, index=False)
    tmp_path.replace(path)
    return combined


def read_events(path: Path) -> List[MacroEvent]:
    """Read an interim events parquet file back into MacroEvent objects."""
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    return dataframe_to_events(df)


def dataframe_to_events(df: pd.DataFrame) -> List[MacroEvent]:
    events: List[MacroEvent] = []
    for row in df.to_dict(orient="records"):
        for field in _DATETIME_FIELDS:
            value = row.get(field)
            if value is not None and hasattr(value, "to_pydatetime"):
                row[field] = value.to_pydatetime()
        for field in _DATE_FIELDS:
            value = row.get(field)
            if value is not None and hasattr(value, "date"):
                row[field] = value.date()
        # NaN (missing) -> None for pydantic
        row = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}
        events.append(MacroEvent(**row))
    return events
