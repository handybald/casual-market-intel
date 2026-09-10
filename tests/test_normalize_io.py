import datetime as dt

from src.data.normalize.io import merge_write_events, read_events
from src.data.schemas import MacroEvent, MacroSource


def _event(event_id, actual):
    return MacroEvent(
        event_id=event_id,
        event_family="NFP",
        indicator="Nonfarm Payrolls",
        release_timestamp_utc=dt.datetime(2024, 1, 5, 13, 30, tzinfo=dt.timezone.utc),
        actual=actual,
        source=MacroSource.MQL5,
        source_timestamp=dt.datetime(2024, 1, 5, 13, 30),
        source_timezone="SERVER",
        retrieval_timestamp_utc=dt.datetime.now(dt.timezone.utc),
    )


def test_merge_write_events_dedups_by_event_id_new_wins(tmp_path):
    path = tmp_path / "events.parquet"
    merge_write_events([_event("e1", 100.0)], path)
    merge_write_events([_event("e1", 216.0), _event("e2", 50.0)], path)

    events = read_events(path)
    assert len(events) == 2
    e1 = next(e for e in events if e.event_id == "e1")
    assert e1.actual == 216.0  # new value won


def test_read_events_roundtrip_preserves_types(tmp_path):
    path = tmp_path / "events.parquet"
    original = _event("e1", 216.0)
    merge_write_events([original], path)

    [loaded] = read_events(path)
    assert loaded.event_id == original.event_id
    assert loaded.actual == original.actual
    assert loaded.release_timestamp_utc == original.release_timestamp_utc
    assert loaded.source == original.source
    assert loaded.timestamp_is_trustworthy is False


def test_read_events_missing_file_returns_empty_list(tmp_path):
    assert read_events(tmp_path / "does_not_exist.parquet") == []
