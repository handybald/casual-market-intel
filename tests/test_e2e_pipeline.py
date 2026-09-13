"""End-to-end regression tests (required test #16): bootstrap -> a
simulated interruption -> resume -> incremental update, run against
real (tmp_path) durable storage, proving no missing history and no
duplicate records across the sequence of runs. Uses mocked transports
throughout -- no real network/credentials needed.
"""
import datetime as dt
from types import SimpleNamespace

import pandas as pd

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import massive as m
from src.data.fetch import forex_factory as ff_fetch
from src.data.event_mapping import load_event_mapping
from src.data.normalize.forex_factory import normalize_forex_factory_file
from src.data.normalize.io import merge_write_events, read_events
from src.data.validation.market import nyse_sessions
from tests._market_fixtures import full_session_bars

GOOD_FF_HTML_TEMPLATE = """
<tr class="calendar__row calendar__row--day-breaker"><td class="calendar__date"><span>{weekday} {month} {day}</span></td></tr>
<tr class="calendar__row">
  <td class="calendar__date"></td><td class="calendar__time">8:30am</td>
  <td class="calendar__currency">USD</td>
  <td class="calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
  <td class="calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
  <td class="calendar__actual">0.{n}%</td><td class="calendar__forecast">0.{n}%</td><td class="calendar__previous">0.{n}%</td>
</tr>
"""


def make_massive_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2020-09-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "massive": {
                "chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "massive"),
                "base_url": "https://api.massive.com", "max_retries": 1, "request_delay_seconds": 0,
                "page_limit": 1000, "adjusted": False, "sort": "asc", "revision_overlap_days": 3,
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def test_massive_partial_month_bootstrap_then_updates_no_gaps_no_duplicates(tmp_path, monkeypatch):
    """Regression test #3: Sept 1-10 bootstrap, then a Sept 11 update,
    then an Oct 5 update -- every day fetched across all three runs must
    end up present exactly once, with September eventually finalized and
    October left provisional (it still contains "today")."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_massive_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        # Genuinely complete (every-minute) bars for every real NYSE
        # session in the requested window -- validation now requires
        # real session completeness (see src/data/validation/market.py),
        # so a single token bar per calendar day would (correctly) fail.
        return full_session_bars(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)
    key = m.cache_key("QQQ", "1min", False)

    # --- Step 1: bootstrap, Sept 1-10, "today" = Sept 10 ---
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10), "1min", today=dt.date(2020, 9, 10))
    entry = manifest.get("massive", key, "2020-09-01", "2020-09-10")
    assert entry.status == "provisional"

    # --- Step 2: update on Sept 11 (dataset_start -> today, as update_data.py does) ---
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 11), "1min", today=dt.date(2020, 9, 11))

    # --- Step 3: update on Oct 5 ---
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 9, 1), dt.date(2020, 10, 5), "1min", today=dt.date(2020, 10, 5))

    sept_entry = manifest.get("massive", key, "2020-09-01", "2020-09-30")
    oct_entry = manifest.get("massive", key, "2020-10-01", "2020-10-05")
    assert sept_entry.status == "complete"   # finalized -- no longer touches "today"
    assert oct_entry.status == "provisional"  # still touches "today" = Oct 5

    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    df = pd.read_parquet(year_path)

    expected_bars = full_session_bars(dt.date(2020, 9, 1), dt.date(2020, 10, 5))
    assert len(df) == len(expected_bars)  # no duplicates, nothing missing
    assert df["timestamp_utc"].is_monotonic_increasing
    assert df["timestamp_utc"].nunique() == len(expected_bars)  # no missing history either

    # Explicitly confirm every real NYSE session in Sept 11-30 (only
    # fetched in step 2/3) is present.
    present_dates = {ts.date() for ts in df["timestamp_utc"]}
    expected_sessions = set(nyse_sessions(dt.date(2020, 9, 1), dt.date(2020, 10, 5)).index.date)
    assert present_dates == expected_sessions
    for session_date in nyse_sessions(dt.date(2020, 9, 11), dt.date(2020, 9, 30)).index.date:
        assert session_date in present_dates


def make_ff_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2024-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "forex_factory": {
                "raw_dir": str(tmp_path / "raw" / "forex_factory"),
                "base_url": "https://www.forexfactory.com/calendar",
                "max_retries": 1, "request_delay_seconds": 0, "display_timezone": "America/New_York",
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def test_forex_factory_bootstrap_interrupted_then_resumed_no_duplicates(tmp_path, monkeypatch):
    """Bootstrap Jan+Feb 2024: February's download "crashes" (simulated
    exception mid-run). Resuming (re-running the same fetch call) must
    NOT lose January's already-durable, checkpointed data, must NOT
    re-fetch January, and must end with exactly one event per (family,
    day) once February succeeds -- no duplicates, nothing missing."""
    config = make_ff_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    event_mapping = load_event_mapping()
    today = dt.date(2024, 3, 1)  # both months are safely in the past

    call_log = []

    def flaky_request(method, url, session=None, max_retries=None, request_delay_seconds=None, **kwargs):
        call_log.append(url)
        if "month=feb.2024" in url:
            raise ConnectionError("simulated network failure fetching February")
        html = GOOD_FF_HTML_TEMPLATE.format(weekday="Mon", month="Jan", day=15, n=3)
        return SimpleNamespace(text=html)

    monkeypatch.setattr(ff_fetch, "request_with_retry", flaky_request)

    # --- "Bootstrap" attempt: Feb fails, Jan succeeds ---
    results = ff_fetch.fetch_forex_factory(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 2, 29), today=today)
    statuses = {(r.year, r.month): r.status for r in results}
    assert statuses[(2024, 1)] == "complete"
    assert statuses[(2024, 2)] == "failed"

    jan_result = next(r for r in results if r.month == 1)
    normalize_forex_factory_file(
        jan_result.path, 2024, 1, event_mapping,
        acquisition_timestamp_utc=dt.datetime.now(dt.timezone.utc), currency_filter="USD",
    )
    jan_events = normalize_forex_factory_file(
        jan_result.path, 2024, 1, event_mapping,
        acquisition_timestamp_utc=dt.datetime.now(dt.timezone.utc), currency_filter="USD",
    )
    events_path = config.interim_root / "macro" / "forex_factory_events.parquet"
    merge_write_events(jan_events, events_path)

    # --- "Resume": re-run the same fetch call. January must be skipped
    # (already checkpointed complete), February retried and now succeeds. ---
    def recovered_request(method, url, session=None, max_retries=None, request_delay_seconds=None, **kwargs):
        call_log.append(url)
        html = GOOD_FF_HTML_TEMPLATE.format(weekday="Thu", month="Feb", day=15, n=4)
        return SimpleNamespace(text=html)

    monkeypatch.setattr(ff_fetch, "request_with_retry", recovered_request)
    calls_before_resume = len(call_log)

    results2 = ff_fetch.fetch_forex_factory(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 2, 29), today=today)
    statuses2 = {(r.year, r.month): r.status for r in results2}
    assert statuses2[(2024, 1)] == "skipped_cached"  # NOT re-downloaded
    assert statuses2[(2024, 2)] == "complete"
    assert len(call_log) == calls_before_resume + 1  # only February was actually requested again

    feb_result = next(r for r in results2 if r.month == 2)
    feb_events = normalize_forex_factory_file(
        feb_result.path, 2024, 2, event_mapping,
        acquisition_timestamp_utc=dt.datetime.now(dt.timezone.utc), currency_filter="USD",
    )
    merge_write_events(feb_events, events_path)

    final_events = read_events(events_path)
    cpi_events = [e for e in final_events if e.event_family == "CPI_MOM"]
    assert len(cpi_events) == 2  # one per month, no duplicates from the two January normalize passes
    event_ids = {e.event_id for e in cpi_events}
    assert len(event_ids) == 2  # each event_id appears exactly once
