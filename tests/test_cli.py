"""CLI-level regression tests for truthful status/exit codes (item #13).

These exercise scripts/fetch_historical_data.py and scripts/update_data.py
directly as Python modules (not subprocesses) with a monkeypatched config
pointed at an isolated tmp_path, so no real network/credentials are needed
for the failure-path assertions.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import AppConfig
import fetch_historical_data as bootstrap
import update_data as update


def make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2024-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"),
            "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"),
            "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "mql5": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "mql5"),
                      "input_csv": str(tmp_path / "raw" / "mql5" / "us_macro_calendar.csv"), "broker_timezone": None},
            "forex_factory": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "forex_factory"),
                               "base_url": "https://www.forexfactory.com/calendar", "max_retries": 1,
                               "request_delay_seconds": 0, "display_timezone": "America/New_York"},
            "massive": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "massive"),
                        "base_url": "https://api.massive.com", "max_retries": 1, "request_delay_seconds": 0,
                        "page_limit": 1000, "adjusted": False, "sort": "asc", "revision_overlap_days": 3},
            "fred": {"raw_dir": str(tmp_path / "raw" / "fred"), "base_url": "https://api.stlouisfed.org/fred",
                     "max_retries": 1, "lookback_buffer_days": 400, "official_series": [
                         {"series_id": "UNRATE", "event_family": "UNEMPLOYMENT_RATE", "transform": "identity", "result_unit": "PERCENT"},
                     ]},
            "bls": {"enabled": False, "raw_dir": str(tmp_path / "raw" / "bls"),
                    "base_url": "https://api.bls.gov/publicAPI/v2", "max_retries": 1,
                    "max_years_per_request": 10, "series": []},
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


@pytest.fixture
def isolated_bootstrap(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(bootstrap, "load_config", lambda: config)
    monkeypatch.setattr(bootstrap, "load_dotenv_if_present", lambda: None)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    return config


@pytest.fixture
def isolated_update(tmp_path, monkeypatch, isolated_bootstrap):
    monkeypatch.setattr(update, "load_config", lambda: isolated_bootstrap)
    monkeypatch.setattr(update, "load_dotenv_if_present", lambda: None)
    return isolated_bootstrap


def test_unknown_source_rejected_not_silently_ignored(isolated_bootstrap):
    with pytest.raises(SystemExit):
        bootstrap.main(["--sources", "totally_bogus_source", "--start", "2024-01-01", "--end", "2024-01-31"])


def test_start_after_end_rejected(isolated_bootstrap):
    with pytest.raises(SystemExit):
        bootstrap.main(["--start", "2024-06-01", "--end", "2024-01-01"])


def test_bls_source_does_not_silently_succeed(isolated_bootstrap):
    """Regression: `--sources bls` used to do nothing and exit 0. Now it's
    a real adapter deliberately configured with 0 series by default --
    still a truthful non-success, not a silent no-op."""
    code = bootstrap.main(["--sources", "bls", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code != 0


def test_bls_with_configured_series_actually_fetches(isolated_bootstrap, monkeypatch):
    from src.data.fetch import bls as bls_fetch
    from types import SimpleNamespace

    isolated_bootstrap._raw["providers"]["bls"]["series"] = ["CUUR0000SA0"]  # noqa: SLF001
    monkeypatch.setattr(
        bls_fetch, "request_with_retry",
        lambda *a, **k: SimpleNamespace(json=lambda: {
            "status": "REQUEST_SUCCEEDED",
            "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": [{"year": "2024", "period": "M01", "periodName": "January", "value": "300.1"}]}]},
        }),
    )

    code = bootstrap.main(["--sources", "bls", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code == 0


def test_missing_massive_credentials_causes_nonzero_exit(isolated_bootstrap):
    code = bootstrap.main(["--sources", "massive", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code != 0


def test_missing_fred_credentials_causes_nonzero_exit(isolated_bootstrap):
    code = bootstrap.main(["--sources", "fred", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code != 0


def test_mql5_missing_exporter_csv_causes_nonzero_exit(isolated_bootstrap):
    """No exporter CSV present -> every requested month is "missing" ->
    the run must not silently report success."""
    code = bootstrap.main(["--sources", "mql5", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code != 0


def test_mql5_success_with_fixture_returns_zero(isolated_bootstrap):
    import shutil

    fixture = Path(__file__).parent / "fixtures" / "mql5_sample.csv"
    csv_path = isolated_bootstrap.provider("mql5")["input_csv"]
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, csv_path)

    code = bootstrap.main(["--sources", "mql5", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code == 0


def test_update_unknown_source_rejected(isolated_update):
    with pytest.raises(SystemExit):
        update.main(["--sources", "not_a_real_source"])


def test_update_bls_does_not_silently_succeed(isolated_update):
    code = update.main(["--sources", "bls", "--end", "2024-01-31"])
    assert code != 0


# -- regression: a successful update through "today" must not report
# failure just because today's window is provisional (second review #2) --

def test_update_successful_provisional_refresh_through_today_exits_zero(isolated_update, monkeypatch):
    import datetime as dt

    from src.data.fetch import massive as massive_fetch
    from tests._market_fixtures import full_session_bars

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    today = dt.date.today()

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        # Validation is now enforced before finalization -- return
        # genuinely complete (every-minute) bars for every real NYSE
        # session in the requested window, or every window would
        # (correctly) fail session-coverage completeness.
        return full_session_bars(start, end)

    monkeypatch.setattr(massive_fetch, "fetch_window_bars", fake_fetch_window)

    code = update.main(["--sources", "massive", "--end", today.isoformat()])
    assert code == 0


def test_update_genuine_massive_failure_still_exits_nonzero(isolated_update, monkeypatch):
    import datetime as dt

    from src.data.fetch import massive as massive_fetch

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    today = dt.date.today()

    def failing_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(massive_fetch, "fetch_window_bars", failing_fetch_window)

    code = update.main(["--sources", "massive", "--end", today.isoformat()])
    assert code != 0


# -- regression: normalized calendar output must respect the requested
# date range, not silently include everything the raw artifact has
# (second review item #9) --

def test_mql5_normalized_output_excludes_rows_outside_requested_range(isolated_bootstrap):
    import datetime as dt
    from pathlib import Path as _Path

    from src.data.normalize.io import read_events

    csv_path = _Path(isolated_bootstrap.provider("mql5")["input_csv"])
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "value_id,event_id,event_name,country_code,currency_code,importance,event_time,period,unit,multiplier,"
        "actual_value,forecast_value,prev_value,revised_prev_value,revision,source_timezone\n"
        "1,1,Unemployment Rate,US,USD,HIGH,2024.01.05 13:30:00,2023.12.01,PERCENT,NONE,3.7,3.8,3.7,,0,SERVER\n"
        "2,2,Unemployment Rate,US,USD,HIGH,2024.02.05 13:30:00,2024.01.01,PERCENT,NONE,3.6,3.7,3.7,,0,SERVER\n",
        encoding="utf-8",
    )

    code = bootstrap.main(["--sources", "mql5", "--start", "2024-01-01", "--end", "2024-01-31"])
    assert code == 0

    events_path = isolated_bootstrap.interim_root / "macro" / "mql5_events.parquet"
    events = read_events(events_path)
    release_dates = {e.source_timestamp.date() for e in events}
    assert len(events) == 1
    assert release_dates == {dt.date(2024, 1, 5)}  # NOT the Feb 5 row


def test_forex_factory_normalized_output_excludes_days_outside_requested_range(isolated_bootstrap, monkeypatch):
    import datetime as dt

    from src.data.fetch import forex_factory as ff_fetch
    from src.data.normalize.io import read_events
    from types import SimpleNamespace

    html = """
    <tr class="calendar__row calendar__row--day-breaker"><td class="calendar__date"><span>Fri Jan 5</span></td></tr>
    <tr class="calendar__row">
      <td class="calendar__date"></td><td class="calendar__time">8:30am</td>
      <td class="calendar__currency">USD</td>
      <td class="calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
      <td class="calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
      <td class="calendar__actual">0.3%</td><td class="calendar__forecast">0.3%</td><td class="calendar__previous">0.3%</td>
    </tr>
    <tr class="calendar__row calendar__row--day-breaker"><td class="calendar__date"><span>Mon Jan 22</span></td></tr>
    <tr class="calendar__row">
      <td class="calendar__date"></td><td class="calendar__time">8:30am</td>
      <td class="calendar__currency">USD</td>
      <td class="calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
      <td class="calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
      <td class="calendar__actual">0.4%</td><td class="calendar__forecast">0.4%</td><td class="calendar__previous">0.4%</td>
    </tr>
    """
    monkeypatch.setattr(ff_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(text=html))

    # Request only the FIRST 10 days of January -- the full month page
    # (fetched regardless, per fetch/forex_factory.py's design) contains
    # both Jan 5 (in range) and Jan 22 (out of range).
    code = bootstrap.main(["--sources", "forex_factory", "--start", "2024-01-01", "--end", "2024-01-10"])
    assert code == 0

    events_path = isolated_bootstrap.interim_root / "macro" / "forex_factory_events.parquet"
    events = read_events(events_path)
    dates = {e.source_timestamp.date() for e in events}
    assert dates == {dt.date(2024, 1, 5)}  # NOT Jan 22
