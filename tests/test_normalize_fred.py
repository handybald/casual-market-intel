import datetime as dt
import json

from src.data.config import AppConfig
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest, ManifestEntry, checksum_bytes
from src.data.normalize.fred import Observation, apply_transform, normalize_fred_official_events
from src.data.schemas import MacroSource, TimestampQuality, ValueUnit


def test_apply_transform_identity_drops_missing_values():
    obs = [Observation(dt.date(2020, 1, 1), 3.7, "2020-02-01"), Observation(dt.date(2020, 2, 1), None, "2020-03-01")]
    result = apply_transform(obs, "identity")
    assert len(result) == 1
    assert result[0].value == 3.7


def test_apply_transform_diff_1():
    obs = [Observation(dt.date(2020, 1, 1), 150000.0, "r1"), Observation(dt.date(2020, 2, 1), 150216.0, "r2")]
    result = apply_transform(obs, "diff_1")
    assert len(result) == 1
    assert result[0].date == dt.date(2020, 2, 1)
    assert result[0].value == 216.0
    assert result[0].realtime_start == "r2"


def test_apply_transform_pct_change_1():
    obs = [Observation(dt.date(2020, 1, 1), 100.0, "r1"), Observation(dt.date(2020, 2, 1), 100.3, "r2")]
    result = apply_transform(obs, "pct_change_1")
    assert round(result[0].value, 4) == 0.3


def test_apply_transform_pct_change_12_requires_twelve_prior_observations():
    obs = [Observation(dt.date(2019, 1, 1) + dt.timedelta(days=30 * i), 100.0 + i, f"r{i}") for i in range(13)]
    result = apply_transform(obs, "pct_change_12")
    assert len(result) == 1  # only index 12 has a full 12-lag partner


def test_apply_transform_skips_zero_base_to_avoid_division_by_zero():
    obs = [Observation(dt.date(2020, 1, 1), 0.0, "r1"), Observation(dt.date(2020, 2, 1), 1.0, "r2")]
    result = apply_transform(obs, "pct_change_1")
    assert result == []


def test_apply_transform_unknown_raises():
    import pytest

    with pytest.raises(ValueError):
        apply_transform([], "not_a_real_transform")


def _make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2020-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "fred": {
                "raw_dir": str(tmp_path / "raw" / "fred"),
                "base_url": "https://api.stlouisfed.org/fred",
                "lookback_buffer_days": 400,
                "official_series": [
                    {"series_id": "PAYEMS", "event_family": "NFP", "transform": "diff_1", "result_unit": "THOUSANDS"},
                ],
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _write_snapshot(config, manifest, series_id, observations, retrieved_at):
    snapshot_dir = config.provider_raw_dir("fred") / series_id
    snapshot_dir.mkdir(parents=True)
    path = snapshot_dir / "snapshot.json"
    payload = {"series_id": series_id, "retrieved_at": retrieved_at.isoformat(), "observations": observations}
    raw_bytes = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw_bytes)
    manifest.record(
        ManifestEntry(
            provider="fred", key=series_id, start="2020-01-01", end="2020-12-31",
            status="complete", rows=len(observations), checksum=checksum_bytes(raw_bytes), path=str(path),
        )
    )
    # normalize_fred_official_events resolves the current snapshot via the
    # "latest_verified.json" pointer (decoupled from the manifest -- see
    # fetch/fred.py), not by scanning manifest entries -- write it here too.
    pointer_path = snapshot_dir / "latest_verified.json"
    pointer_path.write_text(
        json.dumps({"snapshot_file": path.name, "retrieved_at": retrieved_at.isoformat(), "rows": len(observations)}),
        encoding="utf-8",
    )
    return path


def test_normalize_fred_official_events_end_to_end(tmp_path):
    config = _make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    event_mapping = load_event_mapping()
    retrieved_at = dt.datetime(2020, 3, 1, tzinfo=dt.timezone.utc)

    observations = [
        {"date": "2019-12-01", "value": "150000", "realtime_start": "2020-01-03"},
        {"date": "2020-01-01", "value": "150216", "realtime_start": "2020-02-07"},
    ]
    _write_snapshot(config, manifest, "PAYEMS", observations, retrieved_at)

    events = normalize_fred_official_events(config, manifest, event_mapping, dt.date(2020, 1, 1))
    assert len(events) == 1
    e = events[0]
    assert e.event_family == "NFP"
    assert e.indicator == "Nonfarm Payrolls"
    assert e.reference_period == dt.date(2020, 1, 1)
    assert e.official_actual == 216.0
    assert e.official_actual_unit == ValueUnit.THOUSANDS.value
    assert e.official_source == "FRED:PAYEMS"
    assert e.official_vintage_date == dt.date(2020, 2, 7)
    assert e.source == MacroSource.FRED.value
    assert e.release_timestamp_utc is None  # FRED obs date is not a release timestamp
    assert e.timestamp_quality == TimestampQuality.UNRESOLVED.value
    assert e.retrieval_timestamp_utc == retrieved_at


def test_normalize_fred_filters_lookback_buffer_rows_before_start_date(tmp_path):
    config = _make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    event_mapping = load_event_mapping()

    observations = [
        {"date": "2019-11-01", "value": "149800", "realtime_start": "2019-12-06"},
        {"date": "2019-12-01", "value": "150000", "realtime_start": "2020-01-03"},  # -> Dec 2019 output, before start_date
        {"date": "2020-01-01", "value": "150216", "realtime_start": "2020-02-07"},  # -> Jan 2020 output, in range
    ]
    _write_snapshot(config, manifest, "PAYEMS", observations, dt.datetime(2020, 3, 1, tzinfo=dt.timezone.utc))

    events = normalize_fred_official_events(config, manifest, event_mapping, dt.date(2020, 1, 1))
    assert len(events) == 1
    assert events[0].reference_period == dt.date(2020, 1, 1)


def test_normalize_fred_skips_series_with_no_verified_snapshot(tmp_path):
    config = _make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    event_mapping = load_event_mapping()
    events = normalize_fred_official_events(config, manifest, event_mapping, dt.date(2020, 1, 1))
    assert events == []


def test_normalize_fred_official_events_tags_latest_revised_vintage_kind(tmp_path):
    config = _make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    event_mapping = load_event_mapping()
    observations = [
        {"date": "2019-12-01", "value": "150000", "realtime_start": "2020-01-03"},
        {"date": "2020-01-01", "value": "150216", "realtime_start": "2020-02-07"},
    ]
    _write_snapshot(config, manifest, "PAYEMS", observations, dt.datetime(2020, 3, 1, tzinfo=dt.timezone.utc))

    [event] = normalize_fred_official_events(config, manifest, event_mapping, dt.date(2020, 1, 1))
    assert event.official_vintage_kind == "LATEST_REVISED"
    assert event.event_id.endswith(":latest_revised")


# -- ALFRED as-of vintage normalization (second review item #6) --

def test_normalize_fred_asof_events_tags_asof_vintage_kind(tmp_path):
    from src.data.normalize.fred import normalize_fred_asof_events

    snapshot_dir = tmp_path / "asof"
    snapshot_dir.mkdir()
    snapshot_path = snapshot_dir / "snapshot.json"
    as_of = dt.date(2020, 2, 10)
    payload = {
        "retrieved_at": dt.datetime(2020, 2, 10, 12, 0, tzinfo=dt.timezone.utc).isoformat(),
        "observations": [
            {"date": "2019-12-01", "value": "150000", "realtime_start": "2020-01-03"},
            {"date": "2020-01-01", "value": "150175", "realtime_start": "2020-02-07"},  # first-published, pre-revision
        ],
    }
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    event_mapping = load_event_mapping()
    events = normalize_fred_asof_events(event_mapping, "PAYEMS", "NFP", "diff_1", ValueUnit.THOUSANDS, as_of, snapshot_path)

    assert len(events) == 1
    event = events[0]
    assert event.official_vintage_kind == "AS_OF"
    assert event.official_vintage_date == as_of
    assert event.official_actual == 175.0  # 150175 - 150000, the FIRST-published value, not a later revision
    assert ":asof:2020-02-10" in event.event_id


def test_asof_and_latest_revised_events_for_same_period_have_different_event_ids(tmp_path):
    """Regression: distinct event_ids so AS_OF and LATEST_REVISED rows
    for the same period never silently overwrite each other via
    normalize/io.py's event_id-keyed merge/dedup."""
    from src.data.normalize.fred import normalize_fred_asof_events

    config = _make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    event_mapping = load_event_mapping()

    observations = [
        {"date": "2019-12-01", "value": "150000", "realtime_start": "2020-01-03"},
        {"date": "2020-01-01", "value": "150216", "realtime_start": "2020-02-07"},  # today's revised value
    ]
    _write_snapshot(config, manifest, "PAYEMS", observations, dt.datetime(2020, 3, 1, tzinfo=dt.timezone.utc))
    [latest_event] = normalize_fred_official_events(config, manifest, event_mapping, dt.date(2020, 1, 1))

    snapshot_path = tmp_path / "asof_snapshot.json"
    snapshot_path.write_text(json.dumps({
        "retrieved_at": dt.datetime(2020, 2, 10, tzinfo=dt.timezone.utc).isoformat(),
        "observations": [
            {"date": "2019-12-01", "value": "150000", "realtime_start": "2020-01-03"},
            {"date": "2020-01-01", "value": "150175", "realtime_start": "2020-02-07"},  # as first published
        ],
    }), encoding="utf-8")
    [asof_event] = normalize_fred_asof_events(
        event_mapping, "PAYEMS", "NFP", "diff_1", ValueUnit.THOUSANDS, dt.date(2020, 2, 10), snapshot_path
    )

    assert latest_event.event_id != asof_event.event_id
    assert latest_event.reference_period == asof_event.reference_period == dt.date(2020, 1, 1)
    assert latest_event.official_actual == 216.0
    assert asof_event.official_actual == 175.0
