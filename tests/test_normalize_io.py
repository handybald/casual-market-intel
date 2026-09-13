import datetime as dt

from src.data.normalize.io import merge_write_events, read_events
from src.data.schemas import MacroEvent, MacroSource, TimestampQuality


def _event(event_id, actual, retrieval_timestamp_utc=None, release_timestamp_utc="default"):
    ts = dt.datetime(2024, 1, 5, 13, 30, tzinfo=dt.timezone.utc) if release_timestamp_utc == "default" else release_timestamp_utc
    return MacroEvent(
        event_id=event_id,
        event_family="NFP",
        indicator="Nonfarm Payrolls",
        release_timestamp_utc=ts,
        timestamp_quality=TimestampQuality.UNRESOLVED if ts is None else TimestampQuality.CONFIRMED,
        actual=actual,
        source=MacroSource.MQL5,
        source_timestamp=dt.datetime(2024, 1, 5, 13, 30),
        source_timezone="SERVER",
        retrieval_timestamp_utc=retrieval_timestamp_utc or dt.datetime.now(dt.timezone.utc),
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


def test_read_events_missing_file_returns_empty_list(tmp_path):
    assert read_events(tmp_path / "does_not_exist.parquet") == []


def test_quarantined_null_timestamp_roundtrips_as_none(tmp_path):
    path = tmp_path / "events.parquet"
    quarantined = _event("e1", 216.0, release_timestamp_utc=None)
    normal = _event("e2", 50.0)
    merge_write_events([quarantined, normal], path)

    events = {e.event_id: e for e in read_events(path)}
    assert events["e1"].release_timestamp_utc is None
    assert events["e1"].is_quarantined is True
    assert events["e2"].release_timestamp_utc is not None


def test_mixed_null_and_resolved_timestamps_sort_without_error(tmp_path):
    path = tmp_path / "events.parquet"
    events = [
        _event("e1", 1.0, release_timestamp_utc=None),
        _event("e2", 2.0, release_timestamp_utc=dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)),
        _event("e3", 3.0, release_timestamp_utc=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)),
    ]
    merge_write_events(events, path)
    loaded = read_events(path)
    assert len(loaded) == 3


# -- regression: re-normalization must preserve original acquisition time (item #11) --

def test_re_writing_same_event_preserves_explicitly_passed_retrieval_time(tmp_path):
    path = tmp_path / "events.parquet"
    acquired_at = dt.datetime(2024, 1, 1, 9, 0, tzinfo=dt.timezone.utc)

    first_pass = _event("e1", 216.0, retrieval_timestamp_utc=acquired_at)
    merge_write_events([first_pass], path)

    # Simulate re-normalizing the SAME raw artifact later (e.g. a bugfix
    # in the normalizer, re-run without re-fetching) -- acquisition time
    # passed in is still the ORIGINAL fetch time, not "now".
    second_pass = _event("e1", 216.0, retrieval_timestamp_utc=acquired_at)
    merge_write_events([second_pass], path)

    [loaded] = read_events(path)
    assert loaded.retrieval_timestamp_utc == acquired_at
    assert loaded.normalized_at_utc >= acquired_at
