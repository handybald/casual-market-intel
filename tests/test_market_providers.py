"""Provider-independent market-data layer: contract, Massive backward
compatibility, the Alpaca adapter (through the REAL orchestration/manifest/
normalization path, with a fake HTTP session built from Alpaca's documented
response contract), provenance, and no accidental provider mixing."""
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import AppConfig
from src.data.fetch import alpaca as alpaca_fetch
from src.data.fetch import massive as massive_fetch
from src.data.fetch import market_provider as mp
from src.data.manifest import Manifest
from src.data.normalize.market import normalize_market_symbol
from src.data.schemas import MarketBar
from src.features.market_response import MarketProvenanceError, load_market_dataset
from tests._market_fixtures import full_session_bars
import fetch_historical_data as bootstrap
import update_data as update

UTC = dt.timezone.utc


def make_config(tmp_path, alpaca_overrides=None, market_providers=None) -> AppConfig:
    alpaca_cfg = {
        "raw_dir": str(tmp_path / "raw" / "alpaca"),
        "base_url": "https://data.alpaca.markets",
        "feed": "iex",
        "adjustment": "raw",
        "entitlement": "basic",
        "page_limit": 10000,
        "max_retries": 1,
        "request_delay_seconds": 0,
        "revision_overlap_days": 3,
    }
    alpaca_cfg.update(alpaca_overrides or {})
    raw = {
        "historical": {"start_date": "2024-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "providers": market_providers or ["massive"],
                   "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"),
            "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"),
            "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "massive": {"raw_dir": str(tmp_path / "raw" / "massive"), "base_url": "https://api.massive.com",
                        "max_retries": 1, "request_delay_seconds": 0, "page_limit": 50000, "adjusted": False,
                        "sort": "asc", "revision_overlap_days": 3},
            "alpaca": alpaca_cfg,
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


# --------------------------------------------------------------------------- fake Alpaca transport
def _alpaca_bar(b):
    return {"t": b["timestamp_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"), "o": b["open"], "h": b["high"], "l": b["low"],
            "c": b["close"], "v": b["volume"], "n": 3, "vw": b["close"]}


class _Resp:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


class FakeAlpacaSession:
    """Serves documented-shape /v2/stocks/bars pages from a bar universe,
    honouring symbols/start/end/limit/page_token, and records every call."""

    def __init__(self, universe=None, page_size=None, keep=lambda ts: True, status=200, error_body=None):
        self.universe = universe or (lambda start, end: full_session_bars(start, end))
        self.page_size = page_size
        self.keep = keep
        self.status = status
        self.error_body = error_body
        self.calls = []

    def request(self, method, url, timeout=None, params=None, headers=None, **kw):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.status != 200:
            return _Resp(self.status, text=self.error_body or "")
        lo = pd.Timestamp(params["start"])
        hi = pd.Timestamp(params["end"])
        d0, d1 = (lo - pd.Timedelta(days=1)).date(), (hi + pd.Timedelta(days=1)).date()
        bars = [b for b in self.universe(d0, d1) if lo <= b["timestamp_utc"] <= hi and self.keep(b["timestamp_utc"])]
        size = self.page_size or int(params["limit"])
        offset = int(params.get("page_token", "0"))
        page = bars[offset:offset + size]
        nxt = str(offset + size) if offset + size < len(bars) else None
        return _Resp(200, {"bars": {params["symbols"]: [_alpaca_bar(b) for b in page]} if page else {},
                           "next_page_token": nxt, "currency": "USD"})


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def _fetch_alpaca(config, session, start, end, today=dt.date(2026, 10, 3), now=None):
    manifest = Manifest(config.manifest_path)
    provider = mp.get_market_provider("alpaca", config)
    results = mp.fetch_market_symbol(config, manifest, provider, "QQQ", start, end, "1min",
                                     session=session, today=today,
                                     now=now or dt.datetime(2026, 10, 3, 12, tzinfo=UTC))
    return provider, Manifest(config.manifest_path), results


# --------------------------------------------------------------------------- registry / capabilities
def test_registry_and_declared_capabilities(tmp_path):
    config = make_config(tmp_path)
    assert {"massive", "alpaca"} <= set(mp.market_provider_names())
    m = mp.get_market_provider("massive", config).capabilities()
    a = mp.get_market_provider("alpaca", config).capabilities()
    assert (m.feed_scope, m.bar_density) == (mp.FEED_SCOPE_CONSOLIDATED, "dense")
    assert (a.source_label, a.feed, a.feed_scope, a.bar_density) == ("ALPACA", "iex", mp.FEED_SCOPE_SINGLE_VENUE, "sparse")
    assert a.history_start == dt.date(2016, 1, 1)
    assert not any((a.trades, a.quotes, a.bbo, a.l2, a.l3))  # bars only -- declared, not implied
    assert json.dumps(a.to_dict())  # serialisable for reports
    with pytest.raises(KeyError):
        mp.get_market_provider("eodhd", config)  # extension point, not a fake implementation


@pytest.mark.parametrize("feed", [None, "", "boats", "delayed_sip"])
def test_alpaca_feed_must_be_explicit_and_understood(tmp_path, feed):
    config = make_config(tmp_path, {"feed": feed})
    with pytest.raises(ValueError, match="feed"):
        mp.get_market_provider("alpaca", config)


def test_alpaca_rejects_unknown_adjustment(tmp_path):
    with pytest.raises(ValueError, match="adjustment"):
        mp.get_market_provider("alpaca", make_config(tmp_path, {"adjustment": "adjusted"}))


def test_massive_identity_is_unchanged(tmp_path):
    """Existing on-disk data and manifest checkpoints must stay valid."""
    config = make_config(tmp_path)
    p = mp.get_market_provider("massive", config)
    assert p.cache_key("QQQ", "1min") == massive_fetch.cache_key("QQQ", "1min", False) == "QQQ:1min:raw"
    assert p.year_path("QQQ", "1min", 2025) == tmp_path / "raw" / "massive" / "QQQ" / "1min" / "raw" / "2025.parquet"
    assert p.interim_year_path("QQQ", "1min", 2025) == tmp_path / "interim" / "massive" / "QQQ" / "1min" / "raw" / "2025.parquet"
    assert p.request_meta("1min") == {"timeframe": "1min", "adjusted": False}
    assert issubclass(massive_fetch.MassiveResponseError, mp.ProviderResponseError)
    assert massive_fetch.MissingCredentialsError is mp.MissingCredentialsError


def test_alpaca_identity_includes_feed(tmp_path):
    iex = mp.get_market_provider("alpaca", make_config(tmp_path))
    sip = mp.get_market_provider("alpaca", make_config(tmp_path, {"feed": "sip"}))
    assert iex.cache_key("QQQ", "1min") == "QQQ:1min:raw:iex" != sip.cache_key("QQQ", "1min")
    assert iex.year_path("QQQ", "1min", 2024) == tmp_path / "raw" / "alpaca" / "iex" / "QQQ" / "1min" / "raw" / "2024.parquet"
    assert iex.year_path("QQQ", "1min", 2024) != sip.year_path("QQQ", "1min", 2024)


# --------------------------------------------------------------------------- Alpaca response parsing
WIN = alpaca_fetch.ny_dates_to_utc_window(dt.date(2024, 1, 2), dt.date(2024, 1, 2))


def test_parse_maps_fields_and_normalizes_to_utc():
    payload = {"bars": {"QQQ": [
        {"t": "2024-01-02T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "n": 7, "vw": 1.4},
        # offset form + nanosecond precision -- both RFC-3339, both must land on the same UTC instant rules
        {"t": "2024-01-02T09:31:00.000000000-05:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
    ]}, "next_page_token": None}
    bars, token = alpaca_fetch.parse_bars_payload(payload, "QQQ", *WIN)
    assert token is None
    assert bars[0] == {"timestamp_utc": dt.datetime(2024, 1, 2, 14, 30, tzinfo=UTC), "open": 1, "high": 2, "low": 0.5,
                       "close": 1.5, "volume": 100, "vwap": 1.4, "transactions": 7}
    assert bars[1]["timestamp_utc"] == dt.datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
    assert bars[1]["timestamp_utc"].utcoffset() == dt.timedelta(0)
    assert bars[1]["vwap"] is None and bars[1]["transactions"] is None  # absent stays absent


@pytest.mark.parametrize("payload, match", [
    ([], "JSON object"),
    ({"message": "forbidden"}, "forbidden"),
    ({"bars": [1, 2]}, "keyed by symbol"),
    ({"bars": {"SPY": []}}, "unrequested"),
    ({"bars": {"QQQ": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1}]}}, "required"),
    ({"bars": {"QQQ": [{"t": "2024-01-02T14:30:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}}, "no UTC offset"),
    ({"bars": {"QQQ": [{"t": 1704205800000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}}, "RFC-3339"),
    ({"bars": {"QQQ": [{"t": "2024-01-02T14:30:15Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}}, "minute-aligned"),
    ({"bars": {"QQQ": [{"t": "2024-01-03T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}}, "outside"),
    ({"bars": {}, "next_page_token": 5}, "next_page_token"),
])
def test_parse_rejects_malformed_responses(payload, match):
    with pytest.raises(alpaca_fetch.AlpacaResponseError, match=match):
        alpaca_fetch.parse_bars_payload(payload, "QQQ", *WIN)


@pytest.mark.parametrize("bars_value", [None, {}])
def test_parse_empty_bars_is_empty_not_error(bars_value):
    assert alpaca_fetch.parse_bars_payload({"bars": bars_value, "next_page_token": None}, "QQQ", *WIN) == ([], None)


def test_request_uses_explicit_feed_auth_headers_ny_day_bounds_and_pagination(tmp_path):
    config = make_config(tmp_path)
    sess = FakeAlpacaSession(page_size=100)
    bars = alpaca_fetch.fetch_window_bars(config, "QQQ", dt.date(2024, 7, 1), dt.date(2024, 7, 1), 1, "minute",
                                          "iex", "raw", sess, "kid", "sec")
    assert len(bars) == 390 and len(sess.calls) == 4
    first = sess.calls[0]
    assert first["url"] == "https://data.alpaca.markets/v2/stocks/bars"
    assert first["headers"] == {"APCA-API-KEY-ID": "kid", "APCA-API-SECRET-KEY": "sec"}
    p = first["params"]
    assert (p["feed"], p["adjustment"], p["timeframe"], p["symbols"], p["sort"]) == ("iex", "raw", "1Min", "QQQ", "asc")
    # July (EDT, UTC-4): NY midnight = 04:00Z; end is one second before the next NY midnight
    assert (p["start"], p["end"]) == ("2024-07-01T04:00:00Z", "2024-07-02T03:59:59Z")
    assert "page_token" not in p and sess.calls[1]["params"]["page_token"] == "100"
    assert [b["timestamp_utc"] for b in bars] == sorted(b["timestamp_utc"] for b in bars)


def test_winter_day_bounds_follow_dst():
    lo, hi = alpaca_fetch.ny_dates_to_utc_window(dt.date(2024, 1, 2), dt.date(2024, 1, 2))
    assert (lo, hi) == (dt.datetime(2024, 1, 2, 5, tzinfo=UTC), dt.datetime(2024, 1, 3, 5, tzinfo=UTC))


def test_repeated_page_token_is_a_loop_error(tmp_path):
    class Looping(FakeAlpacaSession):
        def request(self, *a, **k):
            self.calls.append(1)
            return _Resp(200, {"bars": {}, "next_page_token": "same"})

    with pytest.raises(alpaca_fetch.AlpacaResponseError, match="repeated"):
        alpaca_fetch.fetch_window_bars(make_config(tmp_path), "QQQ", dt.date(2024, 1, 2), dt.date(2024, 1, 2), 1,
                                       "minute", "iex", "raw", Looping(), "k", "s")


def test_sip_on_basic_plan_caps_request_end_and_validation_as_of(tmp_path):
    p = mp.get_market_provider("alpaca", make_config(tmp_path, {"feed": "sip"}))
    now = dt.datetime(2024, 7, 1, 15, 0, tzinfo=UTC)
    assert p.validation_as_of(now) == now - dt.timedelta(minutes=16)
    sess = FakeAlpacaSession()
    alpaca_fetch.fetch_window_bars(p.config, "QQQ", dt.date(2024, 7, 1), dt.date(2024, 7, 1), 1, "minute", "sip", "raw",
                                   sess, "k", "s", end_cap_utc=p.validation_as_of(now))
    assert sess.calls[0]["params"]["end"] == "2024-07-01T14:43:59Z"
    iex = mp.get_market_provider("alpaca", make_config(tmp_path))
    assert iex.validation_as_of(now) == now  # IEX is real-time on Basic: no cap


# --------------------------------------------------------------------------- orchestration + manifest (Alpaca)
def test_alpaca_full_path_checkpoints_normalizes_and_resumes(tmp_path, creds):
    config = make_config(tmp_path)
    sess = FakeAlpacaSession()
    provider, manifest, results = _fetch_alpaca(config, sess, dt.date(2024, 1, 1), dt.date(2024, 2, 29))
    assert [r.status for r in results] == ["complete", "complete"]
    key = "QQQ:1min:raw:iex"
    entry = manifest.get("alpaca", key, "2024-01-01", "2024-01-31")
    assert entry.status == "complete" and entry.rows > 0 and manifest.verify_artifact(entry)
    assert entry.request_meta["feed"] == "iex" and entry.request_meta["feed_scope"] == "single_venue"
    assert manifest.entries_for("massive") == []  # nothing recorded under another provider
    report = json.loads((tmp_path / "manifests" / "validation_reports" / "alpaca-iex_QQQ_1min_raw_2024-01-01_2024-01-31.json").read_text())
    assert report["bar_density"] == "sparse" and report["provider_capabilities"]["feed"] == "iex"

    # resume: verified chunks are skipped without any request
    calls_before = len(sess.calls)
    _, _, again = _fetch_alpaca(config, sess, dt.date(2024, 1, 1), dt.date(2024, 2, 29))
    assert [r.status for r in again] == ["skipped_cached"] * 2 and len(sess.calls) == calls_before

    frames = normalize_market_symbol(config, "QQQ", dt.date(2024, 1, 1), dt.date(2024, 2, 29),
                                     manifest=Manifest(config.manifest_path), provider=provider)
    df = frames[0]
    assert (df["source"].unique().tolist(), df["feed"].unique().tolist(), df["feed_scope"].unique().tolist()) == (
        ["ALPACA"], ["iex"], ["single_venue"])
    assert str(df["timestamp_utc"].dt.tz) == "UTC"
    out = tmp_path / "interim" / "alpaca" / "iex" / "QQQ" / "1min" / "raw" / "2024.parquet"
    assert out.exists()
    assert df["retrieval_timestamp_utc"].notna().all() and df["raw_artifact_checksum"].notna().all()


def test_switching_feed_never_reuses_other_feeds_checkpoint(tmp_path, creds):
    _fetch_alpaca(make_config(tmp_path), FakeAlpacaSession(), dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    sip_sess = FakeAlpacaSession()
    _, manifest, results = _fetch_alpaca(make_config(tmp_path, {"feed": "sip"}), sip_sess, dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "complete" and sip_sess.calls  # fetched, not skipped
    assert sip_sess.calls[0]["params"]["feed"] == "sip"
    assert {e.key for e in manifest.entries_for("alpaca")} == {"QQQ:1min:raw:iex", "QQQ:1min:raw:sip"}


def test_sparse_iex_minutes_are_not_a_hard_failure_but_zero_bar_session_is(tmp_path, creds):
    # drop every 3rd minute (≈67% fill) -- far below the dense 98% threshold
    sparse = FakeAlpacaSession(keep=lambda ts: ts.minute % 3 != 0)
    _, manifest, results = _fetch_alpaca(make_config(tmp_path), sparse, dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "complete"
    report = json.loads((tmp_path / "manifests" / "validation_reports" / "alpaca-iex_QQQ_1min_raw_2024-01-02_2024-01-05.json").read_text())
    assert report["incomplete_sessions"] and not report["is_clean"] and not report["is_hard_failure"]

    # one real session (Jan 3) with zero bars is still a failed chunk
    hole = FakeAlpacaSession(keep=lambda ts: ts.date() != dt.date(2024, 1, 3))
    _, manifest, results = _fetch_alpaca(make_config(tmp_path / "b"), hole, dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "failed"
    assert "ZERO rows" in manifest.get("alpaca", "QQQ:1min:raw:iex", "2024-01-02", "2024-01-05").error


def test_same_sparse_data_under_dense_massive_policy_fails(tmp_path, monkeypatch):
    """The density policy comes from the provider's declaration -- the
    identical gappy data from a consolidated provider is NOT accepted."""
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setattr(massive_fetch, "fetch_window_bars", lambda cfg, sym, s, e, *a, **k: [
        b for b in full_session_bars(s, e) if b["timestamp_utc"].minute % 3 != 0])
    config = make_config(tmp_path)
    res = massive_fetch.fetch_massive_symbol(config, Manifest(config.manifest_path), "QQQ", dt.date(2024, 1, 2),
                                             dt.date(2024, 1, 5), "1min", today=dt.date(2026, 10, 3))
    assert res[0].status == "failed"


def test_empty_response_on_real_sessions_fails_but_weekend_is_verified_empty(tmp_path, creds):
    empty = FakeAlpacaSession(universe=lambda s, e: [])
    _, manifest, results = _fetch_alpaca(make_config(tmp_path), empty, dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "failed" and "received 0 bars" in results[0].error
    _, _, weekend = _fetch_alpaca(make_config(tmp_path / "w"), empty, dt.date(2024, 1, 6), dt.date(2024, 1, 7))
    assert weekend[0].status == "empty"


def test_window_before_documented_history_is_explicit_gap_without_request(tmp_path, creds):
    sess = FakeAlpacaSession()
    _, manifest, results = _fetch_alpaca(make_config(tmp_path), sess, dt.date(2015, 12, 1), dt.date(2016, 1, 31))
    assert results[0].status == "failed" and "documented history start" in results[0].error
    assert results[1].status == "complete"
    assert len({c["params"]["start"][:7] for c in sess.calls}) == 1  # only January 2016 was requested


def test_entitlement_error_is_recorded_truthfully_with_provider_message(tmp_path, creds):
    body = '{"code":42210000,"message":"subscription does not permit querying recent SIP data"}'
    _, manifest, results = _fetch_alpaca(make_config(tmp_path, {"feed": "sip"}), FakeAlpacaSession(status=403, error_body=body),
                                         dt.date(2024, 1, 2), dt.date(2024, 1, 5))
    assert results[0].status == "failed"
    err = manifest.get("alpaca", "QQQ:1min:raw:sip", "2024-01-02", "2024-01-05").error
    assert "403" in err and "subscription does not permit" in err


def test_partial_window_is_provisional_then_sibling_checksums_stay_valid(tmp_path, creds):
    """Deterministic regression for the Oct 1-3 bug: two provisional
    chunks share one year file; the earlier one must not go stale."""
    config = make_config(tmp_path)
    sess = FakeAlpacaSession()
    _, manifest, results = _fetch_alpaca(config, sess, dt.date(2026, 9, 1), dt.date(2026, 10, 2),
                                         today=dt.date(2026, 10, 3), now=dt.datetime(2026, 10, 3, 12, tzinfo=UTC))
    assert [r.status for r in results] == ["provisional", "provisional"]
    gaps = manifest.classify_gaps("alpaca", "QQQ:1min:raw:iex", dt.date(2026, 9, 1), dt.date(2026, 10, 2),
                                  today=dt.date(2026, 10, 3))
    assert gaps and all(g.reason == "provisional" for g in gaps), gaps


# --------------------------------------------------------------------------- CLI
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    for mod in (bootstrap, update):
        monkeypatch.setattr(mod, "load_config", lambda: config)
        monkeypatch.setattr(mod, "load_dotenv_if_present", lambda: None)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    return config


def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network call expected")
    monkeypatch.setattr(requests.Session, "request", boom)


def test_cli_missing_alpaca_credentials_fails_before_network(isolated, monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setenv("APCA_API_KEY_ID", "only-the-id")
    code = bootstrap.main(["--sources", "alpaca", "--start", "2024-01-02", "--end", "2024-01-05"])
    assert code != 0
    assert "APCA_API_SECRET_KEY not set" in capsys.readouterr().out


def test_cli_alpaca_failure_never_falls_back_to_massive(isolated, monkeypatch, creds):
    monkeypatch.setattr(massive_fetch, "fetch_window_bars",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Massive must not be called")))
    monkeypatch.setattr(requests, "Session", lambda: FakeAlpacaSession(status=403, error_body='{"message":"forbidden"}'))
    code = bootstrap.main(["--sources", "alpaca", "--start", "2024-01-02", "--end", "2024-01-05"])
    assert code != 0
    m = Manifest(isolated.manifest_path)
    assert m.entries_for("massive") == [] and m.entries_for("alpaca")[0].status == "failed"


def test_cli_alpaca_success_and_update_gap_report(isolated, monkeypatch, creds, capsys):
    monkeypatch.setattr(requests, "Session", lambda: FakeAlpacaSession())
    assert bootstrap.main(["--sources", "alpaca", "--start", "2024-01-01", "--end", "2024-01-31"]) == 0
    out = capsys.readouterr().out
    assert "'feed': 'iex'" in out and "'feed_scope': 'single_venue'" in out
    isolated._raw["historical"]["start_date"] = "2024-01-01"  # noqa: SLF001
    # update over a range that is now fully verified exits 0 and reports per-provider gaps
    assert update.main(["--sources", "alpaca", "--end", "2024-01-31"]) == 0


def test_cli_rejects_unknown_source_and_reports_misconfigured_provider(isolated, monkeypatch, creds):
    with pytest.raises(SystemExit):
        bootstrap.main(["--sources", "eodhd"])
    isolated._raw["providers"]["alpaca"]["feed"] = "boats"  # noqa: SLF001
    _no_network(monkeypatch)
    assert bootstrap.main(["--sources", "alpaca", "--start", "2024-01-02", "--end", "2024-01-05"]) != 0


def test_default_sources_follow_config_market_providers(tmp_path):
    assert bootstrap.default_sources(make_config(tmp_path)) == ["mql5", "forex_factory", "massive", "fred"]
    assert bootstrap.default_sources(make_config(tmp_path, market_providers=["massive", "alpaca"])) == [
        "mql5", "forex_factory", "massive", "alpaca", "fred"]
    legacy = make_config(tmp_path)
    del legacy._raw["market"]["providers"]  # noqa: SLF001 - pre-abstraction config shape
    assert legacy.market_providers == ["massive"] and legacy.primary_market_provider == "massive"


# --------------------------------------------------------------------------- canonical schema / provenance
def test_canonical_bar_rejects_naive_and_non_utc_timestamps():
    base = dict(symbol="QQQ", open=1, high=1, low=1, close=1, volume=1,
                retrieval_timestamp_utc=dt.datetime(2024, 1, 1, tzinfo=UTC))
    MarketBar(timestamp_utc=dt.datetime(2024, 1, 2, 14, 30, tzinfo=UTC), **base)
    with pytest.raises(ValueError, match="naive"):
        MarketBar(timestamp_utc=dt.datetime(2024, 1, 2, 14, 30), **base)
    with pytest.raises(ValueError, match="UTC"):
        MarketBar(timestamp_utc=dt.datetime(2024, 1, 2, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5))), **base)


def _write_interim(root, rows):
    d = root / "QQQ" / "1min" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / "2024.parquet", index=False)


def _row(minute, **prov):
    return {"timestamp_utc": pd.Timestamp("2024-01-02 14:30", tz="UTC") + pd.Timedelta(minutes=minute),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, **prov}


def test_market_response_loader_refuses_mixed_or_unexpected_provenance(tmp_path):
    lo, hi = dt.datetime(2024, 1, 2, tzinfo=UTC), dt.datetime(2024, 1, 3, tzinfo=UTC)
    iex = {"source": "ALPACA", "feed": "iex", "feed_scope": "single_venue", "adjustment": "raw", "timeframe": "1min"}
    _write_interim(tmp_path / "mixed", [_row(0, **iex), _row(1, **{**iex, "source": "MASSIVE"})])
    with pytest.raises(MarketProvenanceError, match="mixed source"):
        load_market_dataset("QQQ", lo, hi, tmp_path / "mixed", iex)
    _write_interim(tmp_path / "iex", [_row(0, **iex)])
    with pytest.raises(MarketProvenanceError, match="expected 'consolidated'"):
        load_market_dataset("QQQ", lo, hi, tmp_path / "iex", {**iex, "feed": "consolidated"})
    df, prov = load_market_dataset("QQQ", lo, hi, tmp_path / "iex", iex)
    assert len(df) == 1 and prov == {**iex, "provenance_basis": "row_columns"}
