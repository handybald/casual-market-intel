"""Known market-exception registry: loading/consistency, the window-query API,
classification in validation, and operational-vs-scientific state in the
fetch orchestration (no endless retry of immutable provider gaps, while the
gaps stay visible)."""
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import load_config
from src.data.fetch import market_provider as mp
from src.data.market_exceptions import (
    ExceptionClass, MarketExceptionRegistry, RegistryError, load_registry_for_config,
)
from src.data.validation.market import validate_market_bars
from tests._market_fixtures import full_session_bars
from tests.test_market_providers import FakeAlpacaSession, _fetch_alpaca, make_config

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
AS_OF = dt.datetime(2026, 10, 4, tzinfo=UTC)
COMMITTED = REPO_ROOT / "config" / "market_exceptions.yaml"


def t(date, hhmm, tz=NY):
    return dt.datetime.combine(dt.date.fromisoformat(date), dt.time.fromisoformat(hhmm), tzinfo=tz)


def entry(id, cls, date, start_local, end_local, symbols=("QQQ",), provider="alpaca", feed="sip", retry=False,
          invalidates=True, status="observed"):
    s, e = t(date, start_local), t(date, end_local)
    return {"id": id, "classification": cls, "status": status, "symbols": list(symbols), "provider": provider,
            "feed": feed, "date": date, "timezone": "America/New_York", "start_local": start_local, "end_local": end_local,
            "start_utc": s.astimezone(UTC).isoformat(), "end_utc": e.astimezone(UTC).isoformat(),
            "retry_appropriate": retry, "invalidates_event_window": invalidates, "evidence": "test", "source": "test"}


def registry(*entries, version=1):
    return MarketExceptionRegistry.from_dict({"schema_version": 1, "registry_version": version, "entries": list(entries)})


@pytest.fixture(scope="module")
def committed():
    return MarketExceptionRegistry.load(COMMITTED)


# --------------------------------------------------------------------------- committed seed content
def test_committed_registry_loads_and_is_wired_into_config(committed):
    assert committed.registry_version >= 1 and len(committed.entries) == 14
    assert load_registry_for_config(load_config()).content_sha256() == committed.content_sha256()
    by_cls = {}
    for e in committed.entries:
        by_cls.setdefault(e.classification, []).append(e)
    assert {e.date.isoformat() for e in by_cls[ExceptionClass.MARKET_WIDE_HALT]} == {
        "2020-03-09", "2020-03-12", "2020-03-16", "2020-03-18"}
    assert all(not e.invalidates_event_window and not e.retry_appropriate for e in by_cls[ExceptionClass.MARKET_WIDE_HALT])
    gaps = {(e.symbols[0], e.date.isoformat()) for e in by_cls[ExceptionClass.PROVIDER_GAP]}
    assert gaps == {("QQQ", "2016-02-02"), ("SPY", "2016-02-02"), ("QQQ", "2016-02-22"), ("QQQ", "2018-05-02"),
                    ("QQQ", "2018-05-03"), ("SPY", "2019-08-12"), ("SPY", "2021-05-05"), ("SPY", "2023-06-05")}
    assert all(e.provider == "alpaca" and e.feed == "sip" and e.invalidates_event_window and not e.retry_appropriate
               for e in by_cls[ExceptionClass.PROVIDER_GAP])
    assert all(e.status == "observed" for e in committed.entries)  # evidence level stated, not overstated


def test_provider_gap_lookup_is_symbol_specific(committed):
    q = committed.query_window("QQQ", t("2018-05-02", "13:55"), t("2018-05-02", "15:00"))
    assert q.has_provider_gap and q.invalidates_event_window and q.tags == ("provider_gap_in_window",)
    assert q.entry_ids == ("alpaca-sip-QQQ-2018-05-02-gap1",) and q.hits[0].overlap_minutes == 65
    assert not committed.query_window("SPY", t("2018-05-02", "13:55"), t("2018-05-02", "15:00")).any


def test_shared_qqq_spy_gap_has_symbol_specific_ranges(committed):
    w = (t("2016-02-02", "11:10"), t("2016-02-02", "11:40"))
    q, s = committed.query_window("QQQ", *w), committed.query_window("SPY", *w)
    assert q.has_provider_gap and s.has_provider_gap
    assert (q.hits[0].entry.start_local, q.hits[0].entry.end_local) == ("11:18", "11:27")
    assert (s.hits[0].entry.start_local, s.hits[0].entry.end_local) == ("11:19", "11:28")


def test_market_halt_is_not_a_provider_gap_and_does_not_invalidate(committed):
    q = committed.query_window("SPY", t("2020-03-09", "09:30"), t("2020-03-09", "10:30"))
    assert q.has_market_halt and not q.has_provider_gap and not q.invalidates_event_window
    assert q.tags == ("market_halt_in_window",)
    # the halt is about the market, not the provider: visible for any provider/feed of the observed symbols
    assert committed.query_window("SPY", t("2020-03-09", "09:30"), t("2020-03-09", "10:30"), provider="massive",
                                  feed="consolidated").has_market_halt


def test_sub_threshold_gap_is_registered(committed):
    q = committed.query_window("SPY", t("2021-05-05", "11:00"), t("2021-05-05", "12:00"))
    assert q.has_provider_gap and q.hits[0].overlap_minutes == 5


def test_half_open_intervals_and_non_overlapping_windows(committed):
    gap = committed.entries_for_session("SPY", dt.date(2023, 6, 5))[0]  # [09:52, 09:56) ET
    assert not committed.query_window("SPY", gap.end_utc, gap.end_utc + dt.timedelta(minutes=5)).any
    assert not committed.query_window("SPY", gap.start_utc - dt.timedelta(minutes=5), gap.start_utc).any
    assert committed.query_window("SPY", gap.end_utc - dt.timedelta(minutes=1), gap.end_utc).any
    assert not committed.query_window("SPY", t("2023-06-06", "09:30"), t("2023-06-06", "16:00")).any


def test_timezone_safe_queries(committed):
    ny = committed.query_window("QQQ", t("2016-02-22", "10:00"), t("2016-02-22", "11:30"))
    utc = committed.query_window("QQQ", t("2016-02-22", "10:00").astimezone(UTC), t("2016-02-22", "11:30").astimezone(UTC))
    assert ny.entry_ids == utc.entry_ids == ("alpaca-sip-QQQ-2016-02-22-gap1", "alpaca-sip-QQQ-2016-02-22-gap2")
    with pytest.raises(ValueError, match="timezone-aware"):
        committed.query_window("QQQ", dt.datetime(2016, 2, 22, 15), dt.datetime(2016, 2, 22, 16))


def test_provider_feed_scoping(committed):
    w = (t("2018-05-02", "10:00"), t("2018-05-02", "11:00"))
    assert not committed.query_window("QQQ", *w, provider="alpaca", feed="iex").any
    assert not committed.query_window("QQQ", *w, provider="massive", feed="consolidated").any


def test_exchange_closed_and_no_trade_classes_are_distinct():
    r = registry(entry("closed", "exchange_closed", "2025-01-09", "09:30", "16:00", symbols=("*",), provider="*", feed="*",
                       invalidates=True),
                 entry("notrade", "legitimate_no_trade_interval", "2024-01-03", "10:00", "10:05", symbols=("QQQ",)))
    c = r.query_window("SPY", t("2025-01-09", "09:00"), t("2025-01-09", "10:00"), provider="massive", feed="consolidated")
    assert c.has_exchange_closed and c.tags == ("exchange_closed_in_window",)
    n = r.query_window("QQQ", t("2024-01-03", "09:58"), t("2024-01-03", "10:10"))
    assert n.has_no_trade_interval and not n.has_provider_gap and n.tags == ("no_trade_interval_in_window",)


# --------------------------------------------------------------------------- loader consistency
@pytest.mark.parametrize("mutate, match", [
    (lambda e: e.update(start_utc="2016-02-02T15:18:00+00:00"), "local and UTC must agree"),  # off by an hour (DST/EST slip)
    (lambda e: e.update(provider="*"), "one specific provider"),
    (lambda e: e.update(classification="temporary_fetch_failure", retry_appropriate=False), "always retry_appropriate"),
    (lambda e: e.update(classification="bad_data"), "unknown classification"),
    (lambda e: e.update(status="confirmed-ish"), "status must be one of"),
    (lambda e: e.update(start_utc="2016-02-02T16:18:00"), "timezone-aware"),
    (lambda e: e.update(evidence=""), "'evidence' is required"),
])
def test_loader_rejects_inconsistent_entries(mutate, match):
    e = entry("x", "provider_gap", "2016-02-02", "11:18", "11:27")
    mutate(e)
    with pytest.raises(RegistryError, match=match):
        registry(e)


def test_duplicate_ids_rejected():
    e = entry("x", "provider_gap", "2016-02-02", "11:18", "11:27")
    with pytest.raises(RegistryError, match="duplicate"):
        registry(e, dict(e))


def test_content_hash_is_format_and_order_independent(tmp_path):
    a = entry("a", "provider_gap", "2016-02-02", "11:18", "11:27")
    b = entry("b", "market_wide_halt", "2020-03-09", "09:35", "09:49", symbols=("QQQ", "SPY"), provider="*", feed="*",
              invalidates=False)
    p1, p2 = tmp_path / "1.yaml", tmp_path / "2.yaml"
    p1.write_text(yaml.safe_dump({"schema_version": 1, "registry_version": 3, "entries": [a, b]}))
    p2.write_text("# comment\n" + yaml.safe_dump({"entries": [b, a], "registry_version": 3, "schema_version": 1},
                                                  default_flow_style=True))
    h = MarketExceptionRegistry.load(p1).content_sha256()
    assert h == MarketExceptionRegistry.load(p2).content_sha256()
    assert h != registry(a, b, version=4).content_sha256()
    assert h != registry(a, {**b, "end_local": "09:50", "end_utc": t("2020-03-09", "09:50").astimezone(UTC).isoformat()},
                         version=3).content_sha256()


# --------------------------------------------------------------------------- validation classification
def _df(bars):
    df = pd.DataFrame(bars, columns=mp.BAR_COLUMNS)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df


def _session_without(date, *ranges):
    d = dt.date.fromisoformat(date)
    out = []
    for b in full_session_bars(d, d):
        local = b["timestamp_utc"].astimezone(NY).strftime("%H:%M")
        if not any(a <= local < z for a, z in ranges):
            out.append(b)
    return _df(out)


def _validate(df, date, reg, symbol):
    d = dt.date.fromisoformat(date)
    return validate_market_bars(df, symbol, d, d, as_of=AS_OF, exceptions=reg.for_dataset(symbol, "alpaca", "sip"))


def test_halt_is_not_reported_as_provider_corruption(committed):
    df = _session_without("2020-03-09", ("09:35", "09:49"))
    strict = validate_market_bars(df, "SPY", dt.date(2020, 3, 9), dt.date(2020, 3, 9), as_of=AS_OF)
    assert strict.is_hard_failure  # 376/390 < 98%: without the registry it looks like a data failure
    r = _validate(df, "2020-03-09", committed, "SPY")
    assert not r.is_hard_failure and r.is_clean and r.known_gap_minutes == 0 and r.unexplained_missing_minutes == 0
    assert r.excluded_minutes_by_class == {"market_wide_halt": 14} and r.expected_regular_minutes == 376
    assert r.applied_exception_ids == ["halt-2020-03-09-QQQ-SPY"]


def test_registered_provider_gap_stays_visible(committed):
    df = _session_without("2018-05-02", ("09:31", "16:00"))
    r = _validate(df, "2018-05-02", committed, "QQQ")
    assert not r.is_hard_failure  # explained -> operationally resolvable
    assert not r.is_clean and r.known_gap_minutes == 389 and r.known_gap_sessions == ["2018-05-02"]
    assert any("REGISTERED provider gaps" in i for i in r.issues)
    assert r.sessions_missing_close_edge == []  # the 15:59 minute is inside the registered gap


def test_small_registered_gap_visible_even_above_threshold(committed):
    df = _session_without("2021-05-05", ("11:27", "11:32"))
    strict = validate_market_bars(df, "SPY", dt.date(2021, 5, 5), dt.date(2021, 5, 5), as_of=AS_OF)
    assert strict.is_clean  # 385/390 >= 98%: without the registry this hole is invisible
    r = _validate(df, "2021-05-05", committed, "SPY")
    assert r.known_gap_minutes == 5 and not r.is_clean and not r.is_hard_failure


def test_unexplained_missing_minutes_still_fail_in_a_session_with_known_gaps(committed):
    df = _session_without("2018-05-02", ("09:31", "16:00"))
    df = df[df.timestamp_utc.dt.tz_convert(NY).dt.strftime("%H:%M") != "09:30"]  # the one real bar disappears too
    r = _validate(df, "2018-05-02", committed, "QQQ")
    assert r.is_hard_failure and r.unexplained_missing_minutes == 1


def test_retryable_gap_does_not_explain_missing_data():
    reg = registry(entry("g", "provider_gap", "2024-01-03", "10:00", "11:00", retry=True))
    r = _validate(_session_without("2024-01-03", ("10:00", "11:00")), "2024-01-03", reg, "QQQ")
    assert r.is_hard_failure and r.known_gap_minutes == 0 and r.unexplained_missing_minutes == 60


def test_stale_registry_entry_is_reported(committed):
    r = _validate(_df(full_session_bars(dt.date(2023, 6, 5), dt.date(2023, 6, 5))), "2023-06-05", committed, "SPY")
    assert r.stale_exception_ids == ["alpaca-sip-SPY-2023-06-05-gap1"] and r.known_gap_minutes == 0
    assert any("registry may be stale" in i for i in r.issues)


def test_no_registry_means_unchanged_strict_behaviour():
    df = _session_without("2020-03-09", ("09:35", "09:49"))
    a = validate_market_bars(df, "SPY", dt.date(2020, 3, 9), dt.date(2020, 3, 9), as_of=AS_OF)
    b = validate_market_bars(df, "SPY", dt.date(2020, 3, 9), dt.date(2020, 3, 9), as_of=AS_OF, exceptions=[])
    assert a.to_dict() == b.to_dict() and a.is_hard_failure


# --------------------------------------------------------------------------- operational vs scientific state
GAP = ("10:00", "10:30")  # 2024-01-03, QQQ: 30 of 390 minutes -> 92% -> a hard failure without the registry


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def _gappy_session():
    def keep(ts):
        local = ts.astimezone(NY)
        return not (local.date() == dt.date(2024, 1, 3) and GAP[0] <= local.strftime("%H:%M") < GAP[1])
    return FakeAlpacaSession(keep=keep)


def _config_with_registry(tmp_path, *entries):
    # dense consolidated feed: a 30-minute hole IS a hard failure without the registry
    cfg = make_config(tmp_path, {"feed": "sip", "entitlement": "algo_trader_plus"})
    if entries:
        p = tmp_path / "exceptions.yaml"
        p.write_text(yaml.safe_dump({"schema_version": 1, "registry_version": 1, "entries": list(entries)}))
        cfg._raw["market"]["exception_registry"] = str(p)  # noqa: SLF001
    return cfg


def test_immutable_provider_gap_is_finalized_once_and_not_refetched(tmp_path, creds):
    cfg = _config_with_registry(tmp_path, entry("gap-1", "provider_gap", "2024-01-03", *GAP))
    s1 = _gappy_session()
    _, manifest, res = _fetch_alpaca(cfg, s1, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert res[0].status == "complete_with_known_gaps"
    e = manifest.get("alpaca", "QQQ:1min:raw:sip", "2024-01-01", "2024-01-31")
    assert e.status == "complete_with_known_gaps" and e.known_exception_ids == ["gap-1"]
    assert manifest.is_complete("alpaca", e.key, e.start, e.end)
    assert manifest.classify_gaps("alpaca", e.key, dt.date(2024, 1, 1), dt.date(2024, 1, 31), today=dt.date(2026, 10, 3)) == []
    # second normal run: operationally resolved -> no request at all
    s2 = _gappy_session()
    _, _, res2 = _fetch_alpaca(cfg, s2, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert res2[0].status == "skipped_cached" and s2.calls == []
    # ...but scientifically still incomplete: the stored data still lacks the minutes
    year = mp.get_market_provider("alpaca", cfg).year_path("QQQ", "1min", 2024)
    r = validate_market_bars(pd.read_parquet(year), "QQQ", dt.date(2024, 1, 3), dt.date(2024, 1, 3), as_of=AS_OF,
                             exceptions=load_registry_for_config(cfg).for_dataset("QQQ", "alpaca", "sip"))
    assert r.known_gap_minutes == 30 and not r.is_clean


def test_same_gap_without_registry_stays_failed_and_retried(tmp_path, creds):
    cfg = _config_with_registry(tmp_path)
    _, manifest, res = _fetch_alpaca(cfg, _gappy_session(), dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert res[0].status == "failed"
    s2 = _gappy_session()
    _, _, res2 = _fetch_alpaca(cfg, s2, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert res2[0].status == "failed" and s2.calls  # retried


def test_temporary_fetch_failure_remains_retryable_with_registry(tmp_path, creds):
    cfg = _config_with_registry(tmp_path, entry("gap-1", "provider_gap", "2024-01-03", *GAP))
    _, manifest, res = _fetch_alpaca(cfg, FakeAlpacaSession(status=503, error_body="unavailable"),
                                     dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert res[0].status == "failed" and "503" in res[0].error
    s2 = _gappy_session()
    _, _, res2 = _fetch_alpaca(cfg, s2, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert s2.calls and res2[0].status == "complete_with_known_gaps"


def test_known_gap_sibling_survives_reconciliation(tmp_path, creds):
    cfg = _config_with_registry(tmp_path, entry("gap-1", "provider_gap", "2024-01-03", *GAP))
    _fetch_alpaca(cfg, _gappy_session(), dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    _, manifest, _ = _fetch_alpaca(cfg, _gappy_session(), dt.date(2024, 2, 1), dt.date(2024, 2, 29))
    jan = manifest.get("alpaca", "QQQ:1min:raw:sip", "2024-01-01", "2024-01-31")
    assert jan.status == "complete_with_known_gaps" and manifest.verify_artifact(jan) and jan.known_exception_ids == ["gap-1"]


def test_cli_run_with_only_known_gaps_succeeds_and_reports_them(tmp_path, creds, monkeypatch, capsys):
    import fetch_historical_data as bootstrap
    import requests

    cfg = _config_with_registry(tmp_path, entry("gap-1", "provider_gap", "2024-01-03", *GAP))
    monkeypatch.setattr(bootstrap, "load_config", lambda: cfg)
    monkeypatch.setattr(bootstrap, "load_dotenv_if_present", lambda: None)
    monkeypatch.setattr(requests, "Session", _gappy_session)
    assert bootstrap.main(["--sources", "alpaca", "--start", "2024-01-01", "--end", "2024-01-31"]) == 0
    assert "'months_with_known_gaps': 1" in capsys.readouterr().out


def test_complete_sibling_that_now_needs_a_known_gap_is_not_reblessed(tmp_path):
    """A month recorded plain "complete" whose rows now only pass through a
    registered gap no longer matches its claim -> invalidated, not re-blessed."""
    from src.data.manifest import ManifestEntry
    cfg = _config_with_registry(tmp_path, entry("gap-1", "provider_gap", "2024-01-03", *GAP))
    provider = mp.get_market_provider("alpaca", cfg)
    exc = load_registry_for_config(cfg).for_dataset("QQQ", "alpaca", "sip")
    df = _session_without("2024-01-03", GAP)
    e = ManifestEntry(provider="alpaca", key="QQQ:1min:raw:sip", start="2024-01-03", end="2024-01-03", status="complete",
                      retrieved_at="2024-02-01T00:00:00+00:00", checksum="old", path=str(tmp_path / "f"))
    ok, reason = mp.sibling_still_valid(e, provider, df, 1, exc)
    assert not ok and "registered provider gaps" in reason
    e2 = ManifestEntry(**{**e.__dict__, "status": "complete_with_known_gaps", "known_exception_ids": ["gap-1"]})
    assert mp.sibling_still_valid(e2, provider, df, 1, exc) == (True, "")
