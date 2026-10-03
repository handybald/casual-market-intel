"""Permanent regressions for the independent review's blockers B1-B6 (and
the small secondary findings). Each section reproduces the reviewer's
adversarial case through the real code path."""
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.fetch import market_provider as mp
from src.data.manifest import Manifest, ManifestEntry
from tests._market_fixtures import full_session_bars
from tests.test_market_providers import FakeAlpacaSession, _fetch_alpaca, make_config

UTC = dt.timezone.utc
KEY = "QQQ:1min:raw:iex"


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


# =========================================================================== B1
def _rewrite_keeping(path, keep):
    df = pd.read_parquet(path)
    df[df["timestamp_utc"].map(keep)].reset_index(drop=True).to_parquet(path, index=False)


def test_b1_truncated_provisional_sibling_cannot_regain_trust(tmp_path, creds):
    """Reviewer repro: fetch Sept+Oct (both provisional on Oct 3), corrupt
    September down to ONE bar, refresh only October. September must not
    come back as a trusted provisional window."""
    config = make_config(tmp_path)
    today, now = dt.date(2026, 10, 3), dt.datetime(2026, 10, 3, 12, tzinfo=UTC)
    _fetch_alpaca(config, FakeAlpacaSession(), dt.date(2026, 9, 1), dt.date(2026, 10, 2), today=today, now=now)
    year_path = mp.get_market_provider("alpaca", config).year_path("QQQ", "1min", 2026)
    first_sept = pd.read_parquet(year_path)["timestamp_utc"].min()
    _rewrite_keeping(year_path, lambda t: t.month == 10 or t == first_sept)

    _, manifest, _ = _fetch_alpaca(config, FakeAlpacaSession(), dt.date(2026, 10, 1), dt.date(2026, 10, 2), today=today, now=now)
    sept = manifest.get("alpaca", KEY, "2026-09-01", "2026-09-30")
    assert sept.status == "failed"
    gaps = manifest.classify_gaps("alpaca", KEY, dt.date(2026, 9, 1), dt.date(2026, 10, 2), today=today)
    assert any(g.reason == "failed" and g.start <= dt.date(2026, 9, 2) for g in gaps), gaps


def _entry(status, start, end, retrieved_at, path, checksum="stale"):
    return ManifestEntry(provider="alpaca", key=KEY, start=start, end=end, status=status, rows=1,
                         retrieved_at=retrieved_at, checksum=checksum, path=str(path))


def _bars_df(bars):
    df = pd.DataFrame(bars, columns=mp.BAR_COLUMNS)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df


@pytest.mark.parametrize("density_feed", ["iex", "sip"])
def test_b1_one_remaining_bar_never_requalifies_complete_or_provisional(tmp_path, density_feed):
    """Direct unit check of the reconciliation contract, for both the
    sparse (IEX) and dense (SIP) density policies."""
    provider = mp.get_market_provider("alpaca", make_config(tmp_path, {"feed": density_feed}))
    path = tmp_path / "f.parquet"
    one_bar = _bars_df(full_session_bars(dt.date(2024, 1, 2), dt.date(2024, 1, 2))[:1])
    for status in ("complete", "provisional"):
        ok, reason = mp.sibling_still_valid(
            _entry(status, "2024-01-01", "2024-01-31", "2024-02-05T00:00:00+00:00", path), provider, one_bar, 1)
        assert not ok and "ZERO rows" in reason


def test_b1_provisional_sibling_judged_as_of_its_own_acquisition(tmp_path):
    """A provisional window fetched mid-session is only held to what was
    due at fetch time -- not to bars that were still in the future."""
    provider = mp.get_market_provider("alpaca", make_config(tmp_path, {"feed": "sip", "entitlement": "algo_trader_plus"}))
    retrieved = dt.datetime(2024, 1, 3, 17, 0, tzinfo=UTC)  # Jan 3 session in progress
    bars = [b for b in full_session_bars(dt.date(2024, 1, 2), dt.date(2024, 1, 3)) if b["timestamp_utc"] < retrieved]
    entry = _entry("provisional", "2024-01-02", "2024-01-03", retrieved.isoformat(), tmp_path / "f.parquet")
    assert mp.sibling_still_valid(entry, provider, _bars_df(bars), 1) == (True, "")
    truncated = [b for b in bars if b["timestamp_utc"].date() != dt.date(2024, 1, 2)]
    assert mp.sibling_still_valid(entry, provider, _bars_df(truncated), 1)[0] is False


def test_b1_empty_entry_only_valid_without_expected_sessions(tmp_path):
    provider = mp.get_market_provider("alpaca", make_config(tmp_path))
    empty = _bars_df([])
    weekend = _entry("empty", "2024-01-06", "2024-01-07", "2024-02-01T00:00:00+00:00", tmp_path / "f")
    weekday = _entry("empty", "2024-01-02", "2024-01-03", "2024-02-01T00:00:00+00:00", tmp_path / "f")
    assert mp.sibling_still_valid(weekend, provider, empty, 1)[0]
    assert not mp.sibling_still_valid(weekday, provider, empty, 1)[0]


def test_b1_complete_sibling_with_rows_removed_by_rewrite_is_invalidated(tmp_path):
    provider = mp.get_market_provider("alpaca", make_config(tmp_path))
    manifest = Manifest(tmp_path / "m.json")
    path = tmp_path / "2024.parquet"
    manifest.record(_entry("complete", "2024-01-01", "2024-01-31", "2024-02-05T00:00:00+00:00", path))
    feb_only = _bars_df(full_session_bars(dt.date(2024, 2, 1), dt.date(2024, 2, 2)))
    mp.reverify_and_refresh_siblings(manifest, provider, KEY, path, "new", feb_only, 1)
    assert manifest.get("alpaca", KEY, "2024-01-01", "2024-01-31").status == "failed"


def test_b1_corrupted_provisional_artifact_is_invalidated_before_rewrite(tmp_path, creds):
    config = make_config(tmp_path)
    today, now = dt.date(2026, 10, 3), dt.datetime(2026, 10, 3, 12, tzinfo=UTC)
    _fetch_alpaca(config, FakeAlpacaSession(), dt.date(2026, 9, 1), dt.date(2026, 10, 2), today=today, now=now)
    year_path = mp.get_market_provider("alpaca", config).year_path("QQQ", "1min", 2026)
    year_path.write_bytes(b"not parquet")
    _, manifest, _ = _fetch_alpaca(config, FakeAlpacaSession(), dt.date(2026, 10, 1), dt.date(2026, 10, 2), today=today, now=now)
    assert manifest.get("alpaca", KEY, "2026-09-01", "2026-09-30").status == "failed"
    assert manifest.get("alpaca", KEY, "2026-10-01", "2026-10-02").status == "provisional"


@pytest.mark.parametrize("today", [dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 3)])
def test_b1_month_boundary_fix_still_holds_first_days_of_month(tmp_path, creds, today):
    now = dt.datetime.combine(today, dt.time(23, 0), tzinfo=UTC)
    _, manifest, results = _fetch_alpaca(make_config(tmp_path), FakeAlpacaSession(), dt.date(2026, 9, 1), today,
                                         today=today, now=now)
    assert all(r.status in ("provisional", "empty") for r in results), results
    gaps = manifest.classify_gaps("alpaca", KEY, dt.date(2026, 9, 1), today, today=today)
    assert all(g.reason == "provisional" for g in gaps), gaps


def test_b1_december_january_boundary(tmp_path, creds):
    today = dt.date(2026, 1, 2)
    _, manifest, results = _fetch_alpaca(make_config(tmp_path), FakeAlpacaSession(), dt.date(2025, 12, 1), today,
                                         today=today, now=dt.datetime(2026, 1, 2, 23, tzinfo=UTC))
    assert [r.status for r in results] == ["provisional", "provisional"]
    entries = manifest.entries_for("alpaca", KEY)
    assert {Path(e.path).name for e in entries} == {"2025.parquet", "2026.parquet"}
    assert all(manifest.verify_artifact(e) for e in entries)
    gaps = manifest.classify_gaps("alpaca", KEY, dt.date(2025, 12, 1), today, today=today)
    assert all(g.reason == "provisional" for g in gaps), gaps


# =========================================================================== B2
from src.data.redaction import RedactingLogFilter, redact_secrets  # noqa: E402

SYNTH = "synthSECRETvalue42xyz"  # synthetic -- never a real credential


class _EchoingSession(FakeAlpacaSession):
    """A provider that answers with a malformed payload echoing a secret."""

    def __init__(self, message):
        super().__init__()
        self.message = message

    def request(self, *a, **k):
        from tests.test_market_providers import _Resp
        return _Resp(200, {"message": self.message})  # no "bars": malformed


@pytest.mark.parametrize("message", [
    f"invalid credentials: APCA-API-SECRET-KEY: {SYNTH}",
    f"request was {{'APCA-API-KEY-ID': 'id', 'APCA-API-SECRET-KEY': '{SYNTH}'}}",
    f"echo: {SYNTH}",  # bare value, no field name: caught by exact-value redaction of held credentials
])
def test_b2_provider_secret_never_reaches_logs_or_manifest(tmp_path, monkeypatch, caplog, message):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", SYNTH)
    caplog.set_level(logging.DEBUG)
    _, manifest, results = _fetch_alpaca(make_config(tmp_path), _EchoingSession(message), dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "failed"
    raw_manifest = Path(make_config(tmp_path).manifest_path).read_text()
    assert SYNTH not in raw_manifest and "REDACTED" in raw_manifest
    assert SYNTH not in caplog.text and "REDACTED" in caplog.text
    assert SYNTH not in results[0].error


def test_b2_cli_summary_and_log_handler_do_not_print_secret(tmp_path, monkeypatch, capsys):
    import fetch_historical_data as bootstrap
    import requests

    config = make_config(tmp_path)
    monkeypatch.setattr(bootstrap, "load_config", lambda: config)
    monkeypatch.setattr(bootstrap, "load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", SYNTH)
    monkeypatch.setattr(requests, "Session", lambda: _EchoingSession(f"bad secret_key={SYNTH}"))
    assert bootstrap.main(["--sources", "alpaca", "--start", "2024-01-02", "--end", "2024-01-05"]) != 0
    captured = capsys.readouterr()
    assert SYNTH not in captured.out and SYNTH not in captured.err


@pytest.mark.parametrize("text", [
    f"https://api.massive.com/v2/aggs?adjusted=false&apiKey={SYNTH}&limit=5",
    f"GET /x?apikey={SYNTH}",
    f"GET /x?api_key={SYNTH}",
    f"GET /x?key={SYNTH}",
    f"url-encoded: next%3Fapi_key%3D{SYNTH}",
    f"Authorization: Bearer {SYNTH}",
    f"headers={{'Authorization': 'Basic {SYNTH}'}}",
    f"APCA-API-KEY-ID: {SYNTH}",
    f"APCA-API-SECRET-KEY={SYNTH}",
    f'{{"apiKey": "{SYNTH}", "status": "ERROR"}}',
    f"{{'secret_key': '{SYNTH}'}}",
    f"x-api-key: {SYNTH}",
    f"token={SYNTH}",
    "outer error: " + repr(RuntimeError(f"inner: HTTPError('403 for url: https://h/p?apiKey={SYNTH}')")),
])
def test_b2_pattern_redaction(text):
    out = redact_secrets(text)
    assert SYNTH not in out and "REDACTED" in out


@pytest.mark.parametrize("text", [
    "close=432.1 volume=120034 vwap=431.98 transactions=88",
    "monkey=5 sort_key=asc key_figure=12 keyboard layout",
    "cache key QQQ:1min:raw:iex for 2024-01-02..2024-01-31",
    "expected 21 NYSE trading session(s) but received 0 bars",
    "the key finding was a 0.25% move",
])
def test_b2_does_not_over_redact_ordinary_text(text, monkeypatch):
    for v in ("MASSIVE_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "FRED_API_KEY", "BLS_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert redact_secrets(text) == text


def test_b2_manifest_record_redacts_any_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", SYNTH)
    m = Manifest(tmp_path / "m.json")
    m.record(ManifestEntry(provider="fred", key="CPIAUCSL", start="2024-01-01", end="2024-01-31",
                           status="failed", error=f"boom ?api_key={SYNTH} and again {SYNTH}"))
    assert SYNTH not in (tmp_path / "m.json").read_text()


def test_b2_log_filter_redacts_message_and_traceback():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed: %s", (f"apiKey={SYNTH}",), None)
    try:
        raise ValueError(f"token={SYNTH}")
    except ValueError:
        record.exc_info = sys.exc_info()
    RedactingLogFilter().filter(record)
    assert SYNTH not in record.getMessage() and record.exc_info is None


# =========================================================================== B3
from src.data.fetch import massive as massive_fetch  # noqa: E402
from src.data.normalize.market import CanonicalTimestampError, normalize_market_year  # noqa: E402
from src.data.validation.market import validate_market_bars  # noqa: E402

DAY = dt.date(2024, 1, 2)
AS_OF = dt.datetime(2024, 2, 1, tzinfo=UTC)


def _session_df(shift=dt.timedelta(0), tf=1):
    df = _bars_df(full_session_bars(DAY, DAY, timeframe_minutes=tf))
    df["timestamp_utc"] = df["timestamp_utc"] + shift
    return df


def _validate(df, tf=1):
    return validate_market_bars(df, "QQQ", DAY, DAY, timeframe_minutes=tf, as_of=AS_OF)


def test_b3_valid_minute_grid_is_clean():
    r = _validate(_session_df())
    assert r.is_clean and r.naive_timestamp_count == 0 and r.misaligned_timestamp_count == 0


def test_b3_naive_timestamps_are_a_hard_failure_not_assumed_utc():
    df = _session_df()
    df["timestamp_utc"] = df["timestamp_utc"].dt.tz_localize(None)
    r = _validate(df)
    assert r.naive_timestamp_count == len(df) and r.is_hard_failure
    assert any("timezone-naive" in i for i in r.issues)


@pytest.mark.parametrize("seconds", [15, 30])
def test_b3_off_grid_minute_bars_fail(seconds):
    r = _validate(_session_df(dt.timedelta(seconds=seconds)))
    assert r.misaligned_timestamp_count > 0 and r.is_hard_failure


def test_b3_single_off_grid_bar_fails_even_when_session_otherwise_complete():
    df = _session_df()
    df.loc[100, "timestamp_utc"] = df.loc[100, "timestamp_utc"] + pd.Timedelta(seconds=15)
    r = _validate(df)
    assert r.misaligned_timestamp_count == 1 and r.is_hard_failure


def test_b3_aware_non_utc_input_is_converted_not_rejected():
    df = _session_df()
    df["timestamp_utc"] = df["timestamp_utc"].dt.tz_convert("America/New_York")
    r = _validate(df)
    assert r.naive_timestamp_count == 0 and r.is_clean


def test_b3_coarser_grids_use_the_configured_timeframe():
    assert _validate(_session_df(tf=5), tf=5).is_clean
    off = _session_df(dt.timedelta(minutes=1), tf=5)  # 14:31, 14:36, ... -- whole minutes but off the 5-min grid
    assert _validate(off, tf=5).misaligned_timestamp_count == len(off)
    hourly = _bars_df([{"timestamp_utc": dt.datetime(2024, 1, 2, h, 30, tzinfo=UTC), "open": 1, "high": 1, "low": 1,
                        "close": 1, "volume": 1, "vwap": None, "transactions": None} for h in range(14, 21)])
    assert _validate(hourly, tf=60).misaligned_timestamp_count == 0  # hour anchoring not adjudicated
    hourly.loc[0, "timestamp_utc"] += pd.Timedelta(seconds=30)
    assert _validate(hourly, tf=60).misaligned_timestamp_count == 1  # but whole minutes are


def _write_raw(config, df):
    provider = mp.get_market_provider("massive", config)
    path = provider.year_path("QQQ", "1min", 2024)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return provider


def test_b3_normalization_refuses_naive_and_off_grid_raw(tmp_path):
    config = make_config(tmp_path)
    naive = _session_df()
    naive["timestamp_utc"] = naive["timestamp_utc"].dt.tz_localize(None)
    _write_raw(config, naive)
    with pytest.raises(CanonicalTimestampError, match="1 timezone-naive|390 timezone-naive"):
        normalize_market_year(config, "QQQ", 2024)
    _write_raw(config, _session_df(dt.timedelta(seconds=15)))
    with pytest.raises(CanonicalTimestampError, match="off-grid"):
        normalize_market_year(config, "QQQ", 2024)


def test_b3_normalization_outputs_utc_from_aware_non_utc_raw(tmp_path):
    config = make_config(tmp_path)
    df = _session_df()
    df["timestamp_utc"] = df["timestamp_utc"].dt.tz_convert("America/New_York")
    _write_raw(config, df)
    out = normalize_market_year(config, "QQQ", 2024, acquisition_timestamp_utc=AS_OF)
    assert str(out["timestamp_utc"].dt.tz) == "UTC"
    assert out["timestamp_utc"].iloc[0] == pd.Timestamp("2024-01-02 14:30", tz="UTC")


def test_b3_massive_transport_rejects_naive_string_timestamp():
    with pytest.raises(massive_fetch.MassiveResponseError, match="no UTC offset"):
        massive_fetch._parse_bars_response({"results": [{"t": "2024-01-02T14:30:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})
    ok = massive_fetch._parse_bars_response({"results": [{"t": "2024-01-02T09:30:00-05:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})
    assert ok[0]["timestamp_utc"] == dt.datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


# =========================================================================== B4
from src.features import market_response as mr  # noqa: E402

LO, HI = dt.datetime(2024, 1, 2, tzinfo=UTC), dt.datetime(2024, 1, 3, tzinfo=UTC)
SIP = {"source": "ALPACA", "feed": "sip", "feed_scope": "consolidated", "adjustment": "raw", "timeframe": "1min"}
IEXP = {**SIP, "feed": "iex", "feed_scope": "single_venue"}
MASS = dict(mr.MASSIVE_EXPECTED_PROVENANCE)


def _interim(root, rows):
    d = root / "QQQ" / "1min" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / "2024.parquet", index=False)
    return root


def _r(i, **cols):
    return {"timestamp_utc": pd.Timestamp("2024-01-02 14:30", tz="UTC") + pd.Timedelta(minutes=i),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, **cols}


@pytest.mark.parametrize("prov", [SIP, IEXP])
def test_b4_fully_labelled_feed_is_accepted_with_row_provenance(tmp_path, prov):
    df, got = mr.load_market_dataset("QQQ", LO, HI, _interim(tmp_path, [_r(0, **prov), _r(1, **prov)]), prov)
    assert len(df) == 2 and got == {**prov, "provenance_basis": "row_columns"}


def test_b4_sip_plus_null_feed_is_rejected(tmp_path):
    """Reviewer repro: feed = ["sip", null] with expected SIP was accepted
    and the output provenance came from config."""
    root = _interim(tmp_path, [_r(0, **SIP), _r(1, **{**SIP, "feed": None})])
    with pytest.raises(mr.MarketProvenanceError, match="null 'feed'"):
        mr.load_market_dataset("QQQ", LO, HI, root, SIP)


@pytest.mark.parametrize("bad, match", [
    ({"feed": "iex"}, "mixed feed"),
    ({"source": "MASSIVE"}, "mixed source"),
    ({"adjustment": "split"}, "mixed adjustment"),
])
def test_b4_mixed_provenance_is_rejected(tmp_path, bad, match):
    root = _interim(tmp_path, [_r(0, **SIP), _r(1, **{**SIP, **bad})])
    with pytest.raises(mr.MarketProvenanceError, match=match):
        mr.load_market_dataset("QQQ", LO, HI, root, SIP)


@pytest.mark.parametrize("stored, match", [
    ({**SIP, "source": "MASSIVE"}, "stored source='MASSIVE'"),
    ({**SIP, "adjustment": "all"}, "stored adjustment='all'"),
    (IEXP, "stored feed='iex'"),
])
def test_b4_provider_feed_adjustment_mismatch_is_rejected(tmp_path, stored, match):
    root = _interim(tmp_path, [_r(0, **stored)])
    with pytest.raises(mr.MarketProvenanceError, match=match):
        mr.load_market_dataset("QQQ", LO, HI, root, SIP)


def test_b4_missing_provenance_columns_rejected_for_non_legacy_provider(tmp_path):
    root = _interim(tmp_path, [_r(0, source="ALPACA", adjustment="raw", timeframe="1min")])
    with pytest.raises(mr.MarketProvenanceError, match="missing"):
        mr.load_market_dataset("QQQ", LO, HI, root, SIP)  # no legacy exception for Alpaca
    with pytest.raises(mr.MarketProvenanceError, match="missing"):
        mr.load_market_dataset("QQQ", LO, HI, _interim(tmp_path / "bare", [_r(0)]), MASS, legacy_unlabeled_source="MASSIVE")


def test_b4_legacy_massive_rule_is_explicit_and_narrow(tmp_path):
    legacy = {"source": "MASSIVE", "adjustment": "raw", "timeframe": "1min"}  # what pre-provider files carry
    df, got = mr.load_market_dataset("QQQ", LO, HI, _interim(tmp_path / "ok", [_r(0, **legacy)]), MASS,
                                     legacy_unlabeled_source="MASSIVE")
    assert got["provenance_basis"] == "legacy_massive_unlabeled" and got["feed"] == "consolidated"
    # only when feed columns are ABSENT -- a present-but-null feed is still unknown provenance
    with pytest.raises(mr.MarketProvenanceError, match="null"):
        mr.load_market_dataset("QQQ", LO, HI, _interim(tmp_path / "nul", [_r(0, **{**MASS, "feed": None})]), MASS,
                               legacy_unlabeled_source="MASSIVE")
    # only for the declaring provider's own source label
    with pytest.raises(mr.MarketProvenanceError, match="legacy exception only"):
        mr.load_market_dataset("QQQ", LO, HI, _interim(tmp_path / "alp", [_r(0, **{**legacy, "source": "ALPACA"})]),
                               {**MASS, "source": "ALPACA"}, legacy_unlabeled_source="MASSIVE")
    # and the exception is declared only by Massive
    assert mp.get_market_provider("massive", make_config(tmp_path)).legacy_unlabeled_interim_source == "MASSIVE"
    assert mp.get_market_provider("alpaca", make_config(tmp_path)).legacy_unlabeled_interim_source is None


def test_b4_response_rows_carry_row_provenance_per_symbol():
    bars = pd.DataFrame([_r(i) for i in range(5)]).set_index("timestamp_utc")
    rows = mr.build_market_response([{"trusted_release_timestamp_utc": None}], {"QQQ": bars, "SPY": None},
                                    market_provenance={"QQQ": {**SIP, "provenance_basis": "row_columns"}, "SPY": None})
    by = {r["symbol"]: r for r in rows}
    assert by["QQQ"]["market_data_feed"] == "sip" and by["QQQ"]["market_data_provenance_basis"] == "row_columns"
    assert by["SPY"]["market_data_feed"] is None  # no data -> no provenance claimed


# =========================================================================== B5
import compare_market_providers as compare_cli  # noqa: E402
from src.data.validation.cross_provider import DatasetSide, NoComparableDataError, compare_market_bars  # noqa: E402


def _store_bars(path, bars):
    path.parent.mkdir(parents=True, exist_ok=True)
    _bars_df(bars).to_parquet(path, index=False)


@pytest.fixture
def cmp_env(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(compare_cli, "load_config", lambda: config)
    return tmp_path


def _run_cmp(tmp_path, start, end):
    return compare_cli.main(["--providers", "massive,alpaca", "--symbols", "QQQ", "--start", start, "--end", end,
                             "--report-dir", str(tmp_path / "rep")])


def test_b5_both_sides_empty_in_window_fails_and_names_both(cmp_env, capsys):
    """Reviewer repro: files hold February, January requested -> used to exit 0."""
    feb = full_session_bars(dt.date(2024, 2, 1), dt.date(2024, 2, 2))
    _store_bars(cmp_env / "raw/massive/QQQ/1min/raw/2024.parquet", feb)
    _store_bars(cmp_env / "raw/alpaca/iex/QQQ/1min/raw/2024.parquet", feb)
    assert _run_cmp(cmp_env, "2024-01-02", "2024-01-31") == 1
    out = capsys.readouterr().out
    assert "massive: NO BARS IN WINDOW" in out and "alpaca-iex: NO BARS IN WINDOW" in out
    assert not list((cmp_env / "rep").glob("*.json"))  # no "successful" report written


def test_b5_one_side_empty_vs_one_side_absent_are_distinguished(cmp_env, capsys):
    jan = full_session_bars(dt.date(2024, 1, 2), dt.date(2024, 1, 2))
    _store_bars(cmp_env / "raw/massive/QQQ/1min/raw/2024.parquet", jan)
    _store_bars(cmp_env / "raw/alpaca/iex/QQQ/1min/raw/2024.parquet", full_session_bars(dt.date(2024, 2, 1), dt.date(2024, 2, 1)))
    assert _run_cmp(cmp_env, "2024-01-02", "2024-01-02") == 1
    out = capsys.readouterr().out
    assert "alpaca-iex: NO BARS IN WINDOW" in out and "massive:" not in out.split("NOT COMPARED")[1].split(".")[0]
    (cmp_env / "raw/alpaca/iex/QQQ/1min/raw/2024.parquet").unlink()
    assert _run_cmp(cmp_env, "2024-01-02", "2024-01-02") == 1
    assert "alpaca-iex: NO STORED FILE" in capsys.readouterr().out


def test_b5_library_refuses_to_compare_empty_window():
    a = _bars_df(full_session_bars(dt.date(2024, 2, 1), dt.date(2024, 2, 1)))
    sa = DatasetSide("massive", "MASSIVE", "consolidated", "consolidated", "raw", "1min")
    sb = DatasetSide("alpaca-iex", "ALPACA", "iex", "single_venue", "raw", "1min")
    with pytest.raises(NoComparableDataError) as ei:
        compare_market_bars(a, a, sa, sb, "QQQ", dt.date(2024, 1, 2), dt.date(2024, 1, 31))
    assert ei.value.empty_sides == ["massive", "alpaca-iex"]


# =========================================================================== B6
import validate_data as validate_script  # noqa: E402


def _store_both_feeds(tmp_path, broken_feed):
    """Fetch valid IEX and SIP datasets through the real path, then delete
    one session from `broken_feed`'s stored year file."""
    for feed in ("iex", "sip"):
        _fetch_alpaca(make_config(tmp_path, {"feed": feed, "entitlement": "algo_trader_plus"}), FakeAlpacaSession(),
                      dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    path = tmp_path / "raw" / "alpaca" / broken_feed / "QQQ" / "1min" / "raw" / "2024.parquet"
    _rewrite_keeping(path, lambda t: t.date() != dt.date(2024, 1, 3))


@pytest.mark.parametrize("active, broken", [("iex", "sip"), ("sip", "iex")])
def test_b6_corruption_in_non_active_feed_is_detected(tmp_path, creds, capsys, active, broken):
    _store_both_feeds(tmp_path, broken)
    report_dir = tmp_path / "reports"
    assert validate_script.validate_market(make_config(tmp_path, {"feed": active}), [], report_dir) is True
    out = capsys.readouterr().out
    assert f"[alpaca-{broken}][QQQ][1min][raw][2024]" in out and "HARD FAILURE" in out
    reports = {p.name: json.loads(p.read_text()) for p in report_dir.glob("market_alpaca-*.json")}
    assert set(reports) == {"market_alpaca-iex_QQQ_1min_raw_2024.json", "market_alpaca-sip_QQQ_1min_raw_2024.json"}
    assert reports[f"market_alpaca-{broken}_QQQ_1min_raw_2024.json"]["reports"][0]["is_hard_failure"] is True
    assert reports[f"market_alpaca-{active}_QQQ_1min_raw_2024.json"]["reports"][0]["is_hard_failure"] is False
    # each dataset validated against its own manifest key and its own density policy
    assert reports["market_alpaca-iex_QQQ_1min_raw_2024.json"]["provider_capabilities"]["bar_density"] == "sparse"
    assert reports["market_alpaca-sip_QQQ_1min_raw_2024.json"]["provider_capabilities"]["bar_density"] == "dense"


def test_b6_both_feeds_valid_passes(tmp_path, creds):
    for feed in ("iex", "sip"):
        _fetch_alpaca(make_config(tmp_path, {"feed": feed, "entitlement": "algo_trader_plus"}), FakeAlpacaSession(),
                      dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert validate_script.validate_market(make_config(tmp_path), [], tmp_path / "reports") is False


def test_b6_discovery_ignores_active_config_and_flags_unknown_dirs(tmp_path):
    root = tmp_path / "raw" / "alpaca"
    for d in ("iex", "sip", "mystery"):
        (root / d).mkdir(parents=True)
    alpaca_cls = mp.market_provider_class("alpaca")
    found, unknown = alpaca_cls.stored_datasets(make_config(tmp_path, {"feed": "iex"}))
    assert [p.feed for p in found] == ["iex", "sip"] and [d.name for d in unknown] == ["mystery"]
    assert validate_script.validate_market(make_config(tmp_path), [], tmp_path / "reports") is True


# =========================================================================== secondary findings
def test_regular_session_return_requires_both_endpoints_regular():
    """09:29 -> 09:30 crosses the open: counted in 'all' but not 'regular_session'."""
    pre = {"timestamp_utc": dt.datetime(2024, 1, 2, 14, 29, tzinfo=UTC)}
    bars = [pre] + full_session_bars(DAY, DAY)[:3]  # 14:29, 14:30, 14:31, 14:32
    a = _bars_df([{**b, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 1}
                  for i, b in enumerate(bars)])
    b = a.assign(close=a["close"] * 1.0001)
    sa = DatasetSide("massive", "MASSIVE", "consolidated", "consolidated", "raw", "1min")
    sb = DatasetSide("alpaca-sip", "ALPACA", "sip", "consolidated", "raw", "1min")
    corr = compare_market_bars(a, b, sa, sb, "QQQ", DAY, DAY)["descriptive"]["return_correlation"]
    assert corr["all"]["n"] == 3 and corr["regular_session"]["n"] == 2


def test_artifact_names_carry_dataset_semantics(tmp_path):
    m_raw = mp.get_market_provider("massive", make_config(tmp_path))
    cfg_adj = make_config(tmp_path)
    cfg_adj._raw["providers"]["massive"]["adjusted"] = True  # noqa: SLF001
    m_adj = mp.get_market_provider("massive", cfg_adj)
    iex_raw = mp.get_market_provider("alpaca", make_config(tmp_path))
    iex_split = mp.get_market_provider("alpaca", make_config(tmp_path, {"adjustment": "split"}))
    d0, d1 = dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    assert m_raw.report_name("QQQ", "1min", d0, d1) == "massive_QQQ_2024-01-01_2024-01-31.json"  # legacy default kept
    names = {p.report_name("QQQ", "1min", d0, d1) for p in (m_raw, m_adj, iex_raw, iex_split)}
    names.add(m_raw.report_name("QQQ", "5min", d0, d1))
    assert len(names) == 5
    assert m_raw.response_output_suffix == "" and m_adj.response_output_suffix == "_massive_adjusted"
    assert iex_raw.response_output_suffix == "_alpaca-iex_raw" != iex_split.response_output_suffix


def test_alpaca_asof_is_not_sent_documented_limitation(tmp_path):
    """Alpaca's symbol-rename mapping (`asof`) is left at the provider default
    -- a documented known limitation (README). This pins current behaviour
    so a change to it is a deliberate, reviewed decision."""
    from src.data.fetch import alpaca as alpaca_fetch
    sess = FakeAlpacaSession()
    alpaca_fetch.fetch_window_bars(make_config(tmp_path), "QQQ", DAY, DAY, 1, "minute", "iex", "raw", sess, "k", "s")
    assert all("asof" not in c["params"] for c in sess.calls)
    meta = mp.get_market_provider("alpaca", make_config(tmp_path)).request_meta("1min")
    assert meta["symbol_mapping"] == "alpaca_default_asof_not_sent"


# =========================================================================== second security re-check
# Percent-encoded credentials and redact-before-truncate. Synthetic values only.
from urllib.parse import unquote, unquote_plus  # noqa: E402

from src.data.http_utils import request_with_retry  # noqa: E402

TEST_SECRET = "SYNTHETIC_TEST_SECRET_DO_NOT_USE"
_FRAGMENTS = {TEST_SECRET[i:i + 8] for i in range(len(TEST_SECRET) - 7)}


def _enc(s, upper=True):
    return "".join(("%%%02X" if upper else "%%%02x") % ord(c) for c in s)


def _decoded_layers(text):
    out = [text]
    for decode in (unquote, unquote_plus):
        cur = text
        for _ in range(4):
            cur = decode(cur)
            out.append(cur)
    return out


def _assert_unrecoverable(text, secret=TEST_SECRET, fragments=_FRAGMENTS):
    """Neither the text nor any URL-decoding of it reveals the secret or an
    8+ character fragment of it. Assertion messages never echo the text."""
    for layer in _decoded_layers(text):
        assert secret not in layer, "credential recoverable from sanitized text"
        assert not any(f in layer for f in fragments), "credential fragment recoverable from sanitized text"


@pytest.fixture
def held_secret(monkeypatch):
    monkeypatch.setenv("APCA_API_SECRET_KEY", TEST_SECRET)


ENCODED_CASES = {
    "plain apiKey": f"GET /v2?apiKey={TEST_SECRET}",
    "plain apikey": f"GET /v2?apikey={TEST_SECRET}",
    "plain api_key": f"GET /v2?api_key={TEST_SECRET}",
    "encoded name": f"GET /v2?{_enc('apiKey')}={TEST_SECRET}",
    "encoded value": f"GET /v2?apiKey={_enc(TEST_SECRET)}",
    "encoded name+value": f"GET /v2?{_enc('apiKey')}={_enc(TEST_SECRET)}",
    "encoded name+value lowercase hex": f"GET /v2?{_enc('apiKey', False)}={_enc(TEST_SECRET, False)}",
    "mixed-case hex": f"GET /v2?apiKey={_enc(TEST_SECRET[:16])}{_enc(TEST_SECRET[16:], False)}",
    "partially encoded name": f"GET /v2?api%4Bey={TEST_SECRET}",
    "partially encoded value": f"GET /v2?apiKey={TEST_SECRET[:5]}{_enc(TEST_SECRET[5:9])}{TEST_SECRET[9:]}",
    "encoded '=' and '&'": f"next%3Fa%3D1%26{_enc('api_key')}%3D{_enc(TEST_SECRET)}",
    "double encoded value": f"GET /v2?apiKey={_enc(_enc(TEST_SECRET))}",
    "unnamed encoded held value": f"provider echoed {_enc(TEST_SECRET)} back",
    "known value under innocent name": f"primary_key={TEST_SECRET}",
    "nested URL in exception repr": "outer: " + repr(RuntimeError(
        f"HTTPError('403 for url: https://h/p?{_enc('apiKey')}={_enc(TEST_SECRET)}&limit=5')")),
    "encoded query inside JSON provider text": '{"message": "bad redirect https://h/p?%61piKey%3D' + _enc(TEST_SECRET) + '"}',
}


@pytest.mark.parametrize("label", sorted(ENCODED_CASES))
def test_sec_encoded_credentials_cannot_be_recovered_by_url_decoding(held_secret, label):
    _assert_unrecoverable(redact_secrets(ENCODED_CASES[label]))


def test_sec_encoded_credential_name_with_unknown_value_is_redacted(monkeypatch):
    """Value not held by the process: caught by decoding the encoded field name."""
    for v in ("MASSIVE_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "FRED_API_KEY", "BLS_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    unknown = "UNHELDsyntheticValue123"
    for text in (f"?{_enc('apiKey')}={unknown}", f"?{_enc('secret_key')}={_enc(unknown)}", f"?api%5Fkey={unknown}"):
        out = redact_secrets(text)
        assert all(unknown not in layer for layer in _decoded_layers(out))


def test_sec_fallback_when_encoded_secret_spans_token_delimiters():
    """A credential split around a literal delimiter is invisible per token
    but reconstructible from the whole text: the final check must catch it."""
    secret = "SYNTHETIC,TEST_SECRET_DO_NOT_USE"
    a, b = secret.split(",")
    out = redact_secrets(f"echo {_enc(a)},{_enc(b)} end", extra_secrets=[secret])
    assert all(secret not in layer for layer in _decoded_layers(out))


@pytest.mark.parametrize("text", [
    "monkey=5 keyboard sort_key=asc primary_key=7",
    "https://h/p?q=a%20b&sort_key=x%2Fy",          # percent-encoded but innocent: left byte-identical
    "progress 25% done, move of 0.25%",
    "close=432.1 volume=120034 %ZZ is not an escape",
])
def test_sec_innocent_text_unchanged_even_with_held_secret(held_secret, text):
    assert redact_secrets(text) == text


# -- redact BEFORE truncate (http_utils) --
class _BodyResp:
    status_code = 403

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        import requests
        raise requests.HTTPError("403")


class _BodySession:
    def __init__(self, text):
        self.text = text

    def request(self, *a, **k):
        return _BodyResp(self.text)


def _http_error_text(body):
    import requests
    with pytest.raises(requests.HTTPError) as ei:
        request_with_retry("GET", "https://h/p", session=_BodySession(body), max_retries=1)
    return str(ei.value)


TRUNCATION_BODIES = {
    "secret entirely before boundary": "err " + TEST_SECRET + " " + "x" * 600,
    "secret crossing boundary": "x" * 290 + TEST_SECRET + " tail",
    "secret beginning near boundary": "x" * 297 + TEST_SECRET,
    "secret after long harmless prefix": "x" * 1000 + TEST_SECRET,
    "secret in JSON-like text": '{"status": "ERROR", "message": "' + "y" * 270 + '", "secret_key": "' + TEST_SECRET + '"}',
    "secret in nested provider message": '{"error": {"message": "upstream said: {\\"detail\\": \\"' + "z" * 260
                                         + " token=" + TEST_SECRET + '\\"}"}}',
    "encoded secret crossing boundary": "x" * 280 + "?apiKey=" + _enc(TEST_SECRET),
}


@pytest.mark.parametrize("label", sorted(TRUNCATION_BODIES))
def test_sec_http_body_is_redacted_before_truncation(held_secret, label):
    _assert_unrecoverable(_http_error_text(TRUNCATION_BODIES[label]))


def test_sec_named_unheld_credential_crossing_boundary_is_redacted(monkeypatch):
    for v in ("MASSIVE_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "FRED_API_KEY", "BLS_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    unheld = "UNHELDsyntheticValueXYZ1234567890"
    text = _http_error_text("x" * 285 + "&apiKey=" + unheld)  # realistic query delimiter before the name
    frags = {unheld[i:i + 8] for i in range(len(unheld) - 7)}
    assert not any(f in text for f in frags)


def test_sec_truncation_case_stays_clean_in_manifest_logs_and_cli(tmp_path, monkeypatch, caplog, capsys):
    import fetch_historical_data as bootstrap
    import requests

    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", TEST_SECRET)
    body = "x" * 290 + TEST_SECRET + " " + _enc(TEST_SECRET)
    caplog.set_level(logging.DEBUG)

    _, manifest, results = _fetch_alpaca(make_config(tmp_path / "lib"), FakeAlpacaSession(status=403, error_body=body),
                                         dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "failed"
    _assert_unrecoverable(results[0].error)
    _assert_unrecoverable(Path(make_config(tmp_path / "lib").manifest_path).read_text())
    _assert_unrecoverable(caplog.text)

    config = make_config(tmp_path / "cli")
    monkeypatch.setattr(bootstrap, "load_config", lambda: config)
    monkeypatch.setattr(bootstrap, "load_dotenv_if_present", lambda: None)
    monkeypatch.setattr(requests, "Session", lambda: FakeAlpacaSession(status=403, error_body=body))
    assert bootstrap.main(["--sources", "alpaca", "--start", "2024-01-02", "--end", "2024-01-05"]) != 0
    captured = capsys.readouterr()
    _assert_unrecoverable(captured.out)
    _assert_unrecoverable(captured.err)
    _assert_unrecoverable(Path(config.manifest_path).read_text())
