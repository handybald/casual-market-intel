"""Versioned dataset descriptor + deterministic fingerprint, built through the
real fetch/normalize path on a small synthetic Alpaca SIP dataset."""
import copy
import datetime as dt
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data import dataset_descriptor as dd
from src.data.fetch import market_provider as mp
from src.data.manifest import Manifest
from src.data.market_exceptions import MarketExceptionRegistry, load_registry_for_config
from src.data.normalize.market import normalize_market_symbol
from tests.test_market_exceptions import GAP, _gappy_session, entry
from tests.test_market_providers import _fetch_alpaca, make_config

START, END = dt.date(2024, 1, 1), dt.date(2024, 1, 31)
CREATED = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def _dataset(root: Path, with_registry=True):
    cfg = make_config(root, {"feed": "sip", "entitlement": "algo_trader_plus"})
    if with_registry:
        root.mkdir(parents=True, exist_ok=True)
        p = root / "exceptions.yaml"
        p.write_text(yaml.safe_dump({"schema_version": 1, "registry_version": 1,
                                     "entries": [entry("gap-1", "provider_gap", "2024-01-03", *GAP)]}))
        cfg._raw["market"]["exception_registry"] = str(p)  # noqa: SLF001
    provider = mp.get_market_provider("alpaca", cfg)
    return cfg, provider


def _build(cfg, provider, created=CREATED):
    return dd.build_descriptor(cfg, provider, load_registry_for_config(cfg), "test_dataset", ["QQQ"], START, END,
                               created_at=created)


@pytest.fixture
def built(tmp_path, creds):
    cfg, provider = _dataset(tmp_path / "a")
    _fetch_alpaca(cfg, _gappy_session(), START, END)
    normalize_market_symbol(cfg, "QQQ", START, END, manifest=Manifest(cfg.manifest_path), provider=provider)
    return cfg, provider, _build(cfg, provider)


def test_descriptor_records_identity_policy_coverage_and_checksums(built):
    cfg, provider, d = built
    assert d["source"] == {"provider": "alpaca", "feed": "sip", "feed_scope": "consolidated", "timeframe": "1min",
                           "adjustment": "raw", "bar_timestamp": "interval_start"}
    assert d["source_policy"]["vwap"]["canonical"] == "alpaca/sip" and "massive" in d["source_policy"]["reference_providers"]
    q = d["symbols"]["QQQ"]
    y = q["years"]["2024"]
    assert q["expected_sessions"] == q["represented_sessions"] == 21
    assert y["known_gap_minutes"] == 30 and y["validation"] == "known_provider_gaps" and y["unexplained_missing_minutes"] == 0
    assert y["regular_observed_minutes"] == y["regular_expected_minutes"] - 30
    assert y["raw_content_matches_normalized"] is True and len(y["content_sha256"]) == 64
    assert len(y["artifacts"]["normalized"]["file_sha256"]) == 64
    assert q["manifest_operational_state"]["complete_with_known_gaps_windows"] == ["2024-01-01..2024-01-31"]
    assert d["fingerprint"].startswith("sha256:") and d["fingerprint"] == dd.compute_fingerprint(d)


def test_fingerprint_is_deterministic_and_ignores_wall_clock(built):
    cfg, provider, d = built
    again = _build(cfg, provider, created=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))
    assert again["created_at_utc"] != d["created_at_utc"] and again["fingerprint"] == d["fingerprint"]


def test_fingerprint_survives_renormalization(built):
    """Re-normalizing rewrites normalized_at_utc (file bytes change) -- the data did not."""
    cfg, provider, d = built
    before = d["symbols"]["QQQ"]["years"]["2024"]["artifacts"]["normalized"]["file_sha256"]
    normalize_market_symbol(cfg, "QQQ", START, END, manifest=Manifest(cfg.manifest_path), provider=provider)
    again = _build(cfg, provider)
    assert again["symbols"]["QQQ"]["years"]["2024"]["artifacts"]["normalized"]["file_sha256"] != before
    assert again["fingerprint"] == d["fingerprint"]


def test_fingerprint_is_path_independent(built, tmp_path):
    cfg, provider, d = built
    shutil.copytree(tmp_path / "a", tmp_path / "elsewhere" / "clone")
    cfg2, provider2 = _dataset(tmp_path / "elsewhere" / "clone")
    d2 = _build(cfg2, provider2)
    assert d2["symbols"]["QQQ"]["years"]["2024"]["artifacts"]["normalized"]["path"] != \
        d["symbols"]["QQQ"]["years"]["2024"]["artifacts"]["normalized"]["path"]
    assert d2["fingerprint"] == d["fingerprint"]


def test_changed_yearly_data_changes_fingerprint_and_is_detected(built):
    cfg, provider, d = built
    path = provider.interim_year_path("QQQ", "1min", 2024)
    df = pd.read_parquet(path)
    df["close"] = df["close"].astype(float)
    df.loc[100, "close"] = df.loc[100, "close"] + 0.01
    df.to_parquet(path, index=False)
    changed = _build(cfg, provider)
    assert changed["symbols"]["QQQ"]["years"]["2024"]["content_sha256"] != d["symbols"]["QQQ"]["years"]["2024"]["content_sha256"]
    assert changed["fingerprint"] != d["fingerprint"]
    assert "QQQ 2024: yearly data/coverage differs" in dd.verify_against(d, changed)


def test_row_count_change_changes_fingerprint(built):
    cfg, provider, d = built
    path = provider.interim_year_path("QQQ", "1min", 2024)
    pd.read_parquet(path).iloc[:-1].to_parquet(path, index=False)
    assert _build(cfg, provider)["fingerprint"] != d["fingerprint"]


@pytest.mark.parametrize("edit", [
    lambda d: d["source"].update(feed="iex"),
    lambda d: d["source"].update(provider="massive"),
    lambda d: d["source"].update(adjustment="split"),
    lambda d: d["source"].update(timeframe="5min"),
    lambda d: d["source_policy"]["volume"].update(canonical="massive"),
    lambda d: d["known_exception_registry"].update(registry_version=2),
    lambda d: d["symbols"]["QQQ"].update(provisional_windows=["2024-01-30..2024-01-31"]),
])
def test_scientific_fields_change_fingerprint(built, edit):
    _, _, d = built
    e = copy.deepcopy(d)
    edit(e)
    assert dd.compute_fingerprint(e) != d["fingerprint"]


@pytest.mark.parametrize("edit", [
    lambda d: d.update(created_at_utc="1999-01-01T00:00:00+00:00"),
    lambda d: d["symbols"]["QQQ"]["years"]["2024"]["artifacts"]["normalized"].update(path="/somewhere/else.parquet"),
    lambda d: d["symbols"]["QQQ"]["manifest_operational_state"].update(status_counts={"failed": 1}),
])
def test_non_scientific_fields_do_not_change_fingerprint(built, edit):
    _, _, d = built
    e = copy.deepcopy(d)
    edit(e)
    assert dd.compute_fingerprint(e) == d["fingerprint"]


def test_registry_change_changes_fingerprint(built):
    cfg, provider, d = built
    p = Path(cfg.market_exception_registry_path)
    raw = yaml.safe_load(p.read_text())
    raw["registry_version"] = 2
    p.write_text(yaml.safe_dump(raw))
    assert _build(cfg, provider)["fingerprint"] != d["fingerprint"]


def test_hand_edited_descriptor_is_detected(built):
    _, _, d = built
    e = copy.deepcopy(d)
    e["symbols"]["QQQ"]["total_rows"] += 1  # edited without recomputing the fingerprint
    assert any("edited by hand" in p for p in dd.verify_against(e, d))


def test_content_hash_is_row_order_independent():
    df = pd.DataFrame({"timestamp_utc": pd.to_datetime(["2024-01-02 14:30", "2024-01-02 14:31"], utc=True),
                       "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0],
                       "volume": [5.0, 6.0], "vwap": [1.0, float("nan")], "transactions": [1, None],
                       "source": "ALPACA", "feed": "sip", "feed_scope": "consolidated", "adjustment": "raw", "timeframe": "1min"})
    assert dd.content_sha256(df) == dd.content_sha256(df.iloc[::-1])
    assert dd.content_sha256(df) != dd.content_sha256(df.assign(feed="iex"))


def test_committed_descriptor_is_self_consistent_and_path_free():
    import json
    p = REPO_ROOT / "metadata" / "datasets" / "core_market_alpaca_sip_1min_raw_qqq_spy.json"
    d = json.loads(p.read_text())
    assert d["fingerprint"] == dd.compute_fingerprint(d)
    assert set(d["symbols"]) == {"QQQ", "SPY"} and d["source"]["feed"] == "sip"
    text = p.read_text()
    assert "/Users/" not in text and "\\\\" not in text  # repository-relative artifact identifiers only
    assert d["known_exception_registry"]["content_sha256"] == \
        MarketExceptionRegistry.load(REPO_ROOT / "config" / "market_exceptions.yaml").content_sha256()
