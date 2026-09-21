"""Tests for the calendar-anchored, vintage-aware macro reconciliation
(src/data/validation/macro_events.py + scripts/validate_macro_events.py)."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from src.data.event_mapping import load_event_mapping
from src.data.normalize.io import events_to_dataframe
from src.data.schemas import MacroEvent, MacroSource, TimestampQuality, ValueUnit
from src.data.validation import macro_events as me

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_macro_events as cli  # noqa: E402

MAPPING = load_event_mapping()
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 20, tzinfo=UTC)
START, END = dt.date(2025, 9, 1), dt.date(2025, 9, 30)
REF = dt.date(2025, 8, 1)                                   # the month the data describes
RELEASE = dt.datetime(2025, 9, 11, 12, 30, tzinfo=UTC)      # ...published in September
RELEASE_DATE = RELEASE.date()

PROFILES = {
    ("CPIAUCSL", "CPI_MOM"): me.FredSeriesProfile("CPIAUCSL", "CPI_MOM", "SA", "pct_change_1", "PERCENT"),
    ("CPIAUCNS", "CPI_YOY"): me.FredSeriesProfile("CPIAUCNS", "CPI_YOY", "NSA", "pct_change_12", "PERCENT"),
    ("CPILFESL", "CORE_CPI_MOM"): me.FredSeriesProfile("CPILFESL", "CORE_CPI_MOM", "SA", "pct_change_1", "PERCENT"),
    ("PAYEMS", "NFP"): me.FredSeriesProfile("PAYEMS", "NFP", "SA", "diff_1", "THOUSANDS"),
}
SERIES = {"CPI_MOM": "CPIAUCSL", "CPI_YOY": "CPIAUCNS", "CORE_CPI_MOM": "CPILFESL", "CORE_CPI_YOY": "CPILFENS",
          "NFP": "PAYEMS"}


def indicator(family):
    return MAPPING.by_family(family).indicator


def ff(family="CPI_MOM", actual=0.4, forecast=0.3, prev=0.2, revised=None, ref=REF, ts=RELEASE, unit="PERCENT", tag="a"):
    return MacroEvent(event_id=f"ff:{family}:{tag}", event_family=family, indicator=indicator(family), reference_period=ref,
                      release_timestamp_utc=ts, timestamp_quality=TimestampQuality.CONFIRMED if ts else TimestampQuality.TENTATIVE,
                      actual=actual, actual_unit=ValueUnit(unit), provider_forecast=forecast,
                      provider_forecast_unit=ValueUnit(unit), previous=prev, previous_unit=ValueUnit(unit),
                      revised_previous=revised, source=MacroSource.FOREX_FACTORY, retrieval_timestamp_utc=NOW)


def mq(family="CPI_MOM", actual=0.4, prev=0.2, revised=None, ref=REF, ts=None, server=dt.datetime(2025, 9, 11, 15, 30),
       unit="PERCENT", tag="a"):
    return MacroEvent(event_id=f"mql5:{family}:{tag}", event_family=family, indicator=indicator(family), reference_period=ref,
                      release_timestamp_utc=ts, timestamp_quality=TimestampQuality.CONFIRMED if ts else TimestampQuality.UNRESOLVED,
                      actual=actual, actual_unit=ValueUnit(unit), previous=prev, previous_unit=ValueUnit(unit),
                      revised_previous=revised, provider_forecast=9.9, source=MacroSource.MQL5,
                      source_timestamp=server, retrieval_timestamp_utc=NOW)


def official(family="CPI_MOM", value=0.4, ref=REF, vintage=RELEASE_DATE, unit="PERCENT", series=None, tag="a"):
    """An ALFRED AS_OF row (value as it stood on `vintage`), or a LATEST_REVISED row when vintage is None."""
    kind = "AS_OF" if vintage else "LATEST_REVISED"
    return MacroEvent(event_id=f"fred:{family}:{ref}:{kind}:{vintage}:{tag}", event_family=family, indicator=indicator(family),
                      reference_period=ref, official_actual=value, official_actual_unit=ValueUnit(unit),
                      official_source=f"FRED:{series or SERIES.get(family, 'X')}", official_vintage_kind=kind,
                      official_vintage_date=vintage or dt.date(2026, 9, 20), source=MacroSource.FRED,
                      retrieval_timestamp_utc=NOW)


def latest(family="CPI_MOM", value=0.35, ref=REF, **kw):
    return official(family, value, ref, vintage=None, **kw)


def run(f=(), m=(), r=(), cfg=None, profiles=PROFILES):
    return me.reconcile(list(f), list(m), list(r), START, END, MAPPING, cfg or me.ValidationConfig(), profiles)


def one(res):
    assert len(res.rows) == 1, [x["canonical_event_id"] for x in res.rows]
    return res.rows[0]


# ---- release date vs reference period ----
def test_release_month_differs_from_reference_month_and_joins_the_reference_observation():
    rel = dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC)
    row = one(run([ff("UNEMPLOYMENT_RATE", 4.3, 4.3, 4.2, ts=rel)], [mq("UNEMPLOYMENT_RATE", 4.3, 4.2, ts=rel)],
                  [official("UNEMPLOYMENT_RATE", 4.3, ref=dt.date(2025, 8, 1), vintage=dt.date(2025, 9, 5), series="UNRATE"),
                   official("UNEMPLOYMENT_RATE", 4.4, ref=dt.date(2025, 9, 1), vintage=dt.date(2025, 10, 3), series="UNRATE")]))
    assert row["release_date"] == "2025-09-05" and row["reference_period"] == "2025-08-01"
    assert row["official_reference_period"] == "2025-08-01"        # NOT the September observation
    assert row["official_release_vintage_value"] == 4.3 and row["release_actual_match_status"] == me.EXACT_MATCH
    assert row["canonical_event_id"] == "UNEMPLOYMENT_RATE:2025-08"


# ---- calendar-anchored universe ----
def test_future_fred_observation_does_not_create_missing_mql5_rows():
    res = run([ff()], [mq(ts=RELEASE)],
              [official(value=0.38), official(ref=dt.date(2025, 9, 1), value=0.3, vintage=dt.date(2025, 10, 24)),
               latest(ref=dt.date(2025, 9, 1), value=0.29)])
    row = one(res)                                   # exactly one event: the calendar release
    assert row["reference_period"] == "2025-08-01" and row["source_match_status"] == me.MATCH
    assert not [r for r in res.rows if r["source_match_status"] == me.MISSING_MQL5]
    assert any("never creates release events" in w for w in res.warnings)


def test_fred_alone_produces_no_events():
    assert run([], [], [official(), latest()]).rows == []


# ---- release vintage vs latest revised ----
def test_latest_revised_differs_from_release_vintage_is_diagnostic_not_a_mismatch():
    row = one(run([ff()], [mq(ts=RELEASE)], [official(value=0.3825), latest(value=0.3483)]))
    assert row["source_match_status"] == me.MATCH and row["release_actual_match_status"] == me.ROUNDING_MATCH
    assert row["official_release_vintage_value"] == 0.3825 and row["official_latest_value"] == 0.3483
    assert row["official_latest_minus_release_vintage"] == pytest.approx(-0.0342)
    assert "diagnostic only" in row["notes"]


def test_release_vintage_matches_mql5_actual_exactly():
    row = one(run([ff()], [mq(actual=0.4, ts=RELEASE)], [official(value=0.4)]))
    assert row["release_actual_match_status"] == me.EXACT_MATCH and row["source_match_status"] == me.MATCH
    assert row["actual_minus_official_release_vintage"] == pytest.approx(0)
    assert row["official_vintage_date"] == "2025-09-11" and row["official_series_id"] == "CPIAUCSL"
    assert row["official_source"] == "FRED:CPIAUCSL"


def test_missing_release_vintage_is_unavailable_never_a_mismatch_against_latest():
    row = one(run([ff()], [mq(actual=0.4, ts=RELEASE)], [latest(value=0.1)]))   # latest is far from 0.4
    assert row["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    assert row["release_actual_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    assert row["official_release_vintage_value"] is None and row["official_latest_value"] == 0.1
    assert "diagnostic only" in row["issues"] and row["actual_minus_official_release_vintage"] is None


def test_no_official_data_at_all_is_unavailable():
    row = one(run([ff()], [mq(ts=RELEASE)], []))
    assert row["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE and "no ALFRED as-of row" in row["issues"]


# ---- look-ahead protection ----
def test_lookahead_vintage_is_rejected():
    late = official(value=0.4, vintage=RELEASE_DATE + dt.timedelta(days=40))   # after the next revision cycle
    row = one(run([ff()], [mq(actual=0.4, ts=RELEASE)], [late]))
    assert row["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    assert row["official_release_vintage_value"] is None and "later than the release" in row["issues"]


def test_vintage_before_release_is_rejected():
    early = official(value=0.4, vintage=RELEASE_DATE - dt.timedelta(days=1))
    row = one(run([ff()], [mq(actual=0.4, ts=RELEASE)], [early]))
    assert row["official_release_vintage_value"] is None and "predate the release" in row["issues"]


def test_earliest_valid_vintage_wins_and_later_ones_are_never_used():
    rows = [official(value=0.4, vintage=RELEASE_DATE + dt.timedelta(days=1)),
            official(value=0.9, vintage=RELEASE_DATE + dt.timedelta(days=2)),
            official(value=0.7, vintage=RELEASE_DATE + dt.timedelta(days=30))]
    row = one(run([ff()], [mq(actual=0.4, ts=RELEASE)], rows))
    assert row["official_release_vintage_value"] == 0.4 and row["official_vintage_date"] == "2025-09-12"


def test_lag_window_is_configurable():
    v = official(value=0.4, vintage=RELEASE_DATE + dt.timedelta(days=5))
    assert one(run([ff()], [mq(ts=RELEASE)], [v]))["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    wide = me.ValidationConfig(max_vintage_lag_days=7)
    assert one(run([ff()], [mq(ts=RELEASE)], [v], cfg=wide))["release_actual_match_status"] == me.EXACT_MATCH


def test_conflicting_vintages_on_same_date_are_ambiguous():
    row = one(run([ff()], [mq(ts=RELEASE)], [official(value=0.4, tag="1"), official(value=0.5, tag="2")]))
    assert row["source_match_status"] == me.AMBIGUOUS_MATCH


# ---- units ----
def test_nfp_is_thousands_and_comparable_to_official():
    rel = dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC)
    row = one(run([ff("NFP", 22, 75, 73, revised=79, unit="THOUSANDS", ts=rel)],
                  [mq("NFP", 22, 73, revised=79, ts=rel, unit="THOUSANDS")],
                  [official("NFP", 22, vintage=dt.date(2025, 9, 5), unit="THOUSANDS", series="PAYEMS"),
                   latest("NFP", -70, unit="THOUSANDS", series="PAYEMS")]))
    assert row["unit_status"] == "MATCH" and row["mql5_unit"] == "THOUSANDS" and row["official_unit"] == "THOUSANDS"
    assert row["release_actual_match_status"] == me.EXACT_MATCH and row["source_match_status"] == me.MATCH
    assert row["actual_minus_forecast"] == -53
    assert MAPPING.by_family("NFP").value_unit == "THOUSANDS"


def test_unknown_unit_is_unverified_and_not_compared():
    row = one(run([ff("NFP", 22, 75, 73, unit="THOUSANDS")], [mq("NFP", 22, 73, ts=RELEASE, unit="UNKNOWN")],
                  [official("NFP", 22, unit="THOUSANDS", series="PAYEMS")]))
    assert row["source_match_status"] == me.UNIT_UNVERIFIED and row["unit_status"] == "UNVERIFIED"
    assert row["release_actual_match_status"] == me.NOT_COMPARABLE and row["actual_minus_forecast"] is None


def test_unit_mismatch_never_coerced():
    row = one(run([ff("NFP", 22, 75, 73, unit="THOUSANDS")], [mq("NFP", 22, 73, ts=RELEASE, unit="LEVEL")], []))
    assert row["source_match_status"] == me.UNIT_MISMATCH and row["actual_minus_forecast"] is None


def test_percent_vs_level_official_is_unit_mismatch():
    row = one(run([ff()], [mq(ts=RELEASE)], [official(value=310.2, unit="LEVEL")]))
    assert row["source_match_status"] == me.UNIT_MISMATCH and row["release_actual_match_status"] == me.NOT_COMPARABLE


# ---- previous vs revised_previous ----
def test_previous_and_revised_previous_are_never_merged():
    rel = dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC)
    row = one(run([ff("NFP", 22, 75, 73, revised=79, unit="THOUSANDS", ts=rel)],
                  [mq("NFP", 22, 73, revised=79, ts=rel, unit="THOUSANDS")], []))
    assert (row["mql5_previous"], row["mql5_revised_previous"]) == (73, 79)
    assert (row["ff_previous"], row["ff_revised_previous"]) == (73, 79)
    assert row["mql5_previous_differs_from_revised"] is True


def test_revised_previous_mismatch_between_calendars_flagged():
    row = one(run([ff("NFP", 22, 75, 73, revised=79, unit="THOUSANDS")], [mq("NFP", 22, 73, revised=80, ts=RELEASE, unit="THOUSANDS")], []))
    assert row["source_match_status"] == me.VALUE_MISMATCH and "revised_previous" in row["issues"]


# ---- exact / rounding / genuine mismatch ----
@pytest.mark.parametrize("actual,off,expected", [
    (0.4, 0.4, me.EXACT_MATCH),
    (0.4, 0.3825, me.ROUNDING_MATCH),       # rounds half-up to 0.4
    (0.3, 0.346, me.ROUNDING_MATCH),
    (0.3, 0.35, me.VALUE_MISMATCH),         # 0.35 rounds half-up to 0.4, not 0.3
    (0.4, 0.348, me.VALUE_MISMATCH),        # rounds to 0.3 -- not tolerance-hidden
    (0.4, 0.9, me.VALUE_MISMATCH),
])
def test_exact_rounding_and_genuine_mismatch(actual, off, expected):
    row = one(run([ff(actual=actual)], [mq(actual=actual, ts=RELEASE)], [official(value=off)]))
    assert row["release_actual_match_status"] == expected
    assert row["source_match_status"] == (me.VALUE_MISMATCH if expected == me.VALUE_MISMATCH else me.MATCH)
    if expected == me.VALUE_MISMATCH:
        assert "VALUE_MISMATCH:actual" in row["issues"]


def test_rounding_never_applies_to_the_latest_revised_value():
    # latest (0.4) equals the actual, the release vintage (0.9) does not: mismatch, latest cannot rescue it
    row = one(run([ff()], [mq(actual=0.4, ts=RELEASE)], [official(value=0.9), latest(value=0.4)]))
    assert row["release_actual_match_status"] == me.VALUE_MISMATCH


def test_ff_vs_mql5_actual_mismatch():
    row = one(run([ff(actual=0.5)], [mq(actual=0.4, ts=RELEASE)], [official(value=0.4)]))
    assert row["source_match_status"] == me.VALUE_MISMATCH and "FF actual 0.5 vs MQL5 0.4" in row["issues"]


# ---- semantic joins ----
def test_cpi_mom_and_yoy_are_never_joined():
    res = run([ff("CPI_MOM", 0.4)], [mq("CPI_YOY", 2.9, 2.7)], [])
    assert {r["event_family"]: r["source_match_status"] for r in res.rows} == {
        "CPI_MOM": me.MISSING_MQL5, "CPI_YOY": me.MISSING_FF}


def test_core_and_headline_cpi_are_never_joined():
    res = run([ff("CORE_CPI_MOM", 0.3)], [mq("CPI_MOM", 0.4)], [])
    assert {r["event_family"]: r["source_match_status"] for r in res.rows} == {
        "CORE_CPI_MOM": me.MISSING_MQL5, "CPI_MOM": me.MISSING_FF}


def test_official_row_of_another_family_is_not_used():
    row = one(run([ff("CPI_MOM")], [mq("CPI_MOM", ts=RELEASE)], [official("CPI_YOY", 2.9, series="CPIAUCNS")]))
    assert row["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE


def test_fred_series_scope_adjustment_and_measure_mismatch_detected():
    bad = {("CPILFESL", "CPI_MOM"): me.FredSeriesProfile("CPILFESL", "CPI_MOM", "SA", "pct_change_1", "PERCENT")}
    row = one(run([ff()], [mq(ts=RELEASE)], [official(value=0.4, series="CPILFESL")], profiles=bad))
    assert row["source_match_status"] == me.SEMANTIC_MISMATCH and "CORE but CPI_MOM is HEADLINE" in row["issues"]
    nsa = {("CPIAUCSL", "CPI_MOM"): me.FredSeriesProfile("CPIAUCSL", "CPI_MOM", "NSA", "pct_change_1", "PERCENT")}
    assert "NSA but CPI_MOM is conventionally SA" in one(run([ff()], [mq(ts=RELEASE)], [official(value=0.4)], profiles=nsa))["issues"]
    yoy = {("CPIAUCSL", "CPI_MOM"): me.FredSeriesProfile("CPIAUCSL", "CPI_MOM", "SA", "pct_change_12", "PERCENT")}
    assert "yields YOY" in one(run([ff()], [mq(ts=RELEASE)], [official(value=0.4)], profiles=yoy))["issues"]


def test_mislabeled_row_is_semantic_mismatch():
    wrong = mq("CPI_MOM", 0.4, ts=RELEASE).model_copy(update={"indicator": "CPI YoY"})
    row = one(run([ff()], [wrong], []))
    assert row["source_match_status"] == me.SEMANTIC_MISMATCH and "labeled 'CPI YoY'" in row["issues"]


# ---- timestamps ----
def test_unresolved_broker_timezone_stays_unverifiable_with_diagnostic_offset():
    row = one(run([ff()], [mq(ts=None)], [official(value=0.4)]))
    assert row["timestamp_status"] == "UNVERIFIABLE" and row["source_match_status"] == me.MATCH
    assert row["mql5_implied_server_utc_offset_hours"] == 3.0            # diagnostic only, never applied
    assert row["release_timestamp_source"].startswith("ff.")
    assert row["release_date_basis"] == "forex_factory.release_timestamp_utc"


def test_explicit_broker_timezone_resolves_timestamp():
    cfg = me.ValidationConfig(mql5_broker_timezone="Europe/Helsinki")   # UTC+3 in September (DST)
    row = one(run([ff()], [mq(ts=None)], [official(value=0.4)], cfg=cfg))
    assert row["timestamp_status"] == "MATCH" and "operator-supplied" in row["release_timestamp_source"]
    wrong = me.ValidationConfig(mql5_broker_timezone="UTC")
    bad = one(run([ff()], [mq(ts=None)], [official(value=0.4)], cfg=wrong))
    assert bad["timestamp_status"] == "MISMATCH" and bad["source_match_status"] == me.TIMESTAMP_MISMATCH
    assert bad["timestamp_delta_seconds"] == 3 * 3600


def test_timestamp_tolerance():
    near = one(run([ff()], [mq(ts=RELEASE + dt.timedelta(seconds=30))], [official(value=0.4)]))
    assert near["timestamp_status"] == "MATCH" and near["timestamp_delta_seconds"] == 30
    far = one(run([ff()], [mq(ts=RELEASE + dt.timedelta(minutes=5))], [official(value=0.4)]))
    assert far["source_match_status"] == me.TIMESTAMP_MISMATCH
    wide = me.ValidationConfig(timestamp_tolerance_seconds=600)
    assert one(run([ff()], [mq(ts=RELEASE + dt.timedelta(minutes=5))], [official(value=0.4)], cfg=wide))["timestamp_status"] == "MATCH"


# ---- presence / ambiguity / scope ----
def test_missing_calendar_source_is_reported_not_dropped():
    assert one(run([ff()], [], [official(value=0.4)]))["source_match_status"] == me.MISSING_MQL5
    assert one(run([], [mq(ts=RELEASE)], [official(value=0.4)]))["source_match_status"] == me.MISSING_FF


def test_duplicate_calendar_rows_are_ambiguous():
    row = one(run([ff(tag="1"), ff(tag="2")], [mq(ts=RELEASE)], [official(value=0.4)]))
    assert row["source_match_status"] == me.AMBIGUOUS_MATCH and row["ff_actual"] is None
    assert "2 Forex Factory and 1 MQL5" in row["issues"]
    assert one(run([], [mq(tag="1", ts=RELEASE), mq(tag="2", ts=RELEASE)], []))["source_match_status"] == me.AMBIGUOUS_MATCH


def test_duplicate_latest_rows_are_ambiguous():
    row = one(run([ff()], [mq(ts=RELEASE)], [official(value=0.4), latest(value=0.4, tag="1"), latest(value=0.5, tag="2")]))
    assert row["source_match_status"] == me.AMBIGUOUS_MATCH


def test_reference_less_rows_match_by_date_but_cannot_join_official():
    row = one(run([ff(ref=None)], [mq(ref=None, ts=RELEASE)], [official(value=0.4)]))
    assert row["reference_period"] is None and row["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    assert "no reference_period" in row["issues"]


def test_out_of_window_and_out_of_scope_releases_ignored():
    other = ff(ts=dt.datetime(2025, 10, 15, 12, 30, tzinfo=UTC), ref=dt.date(2025, 9, 1))
    assert run([other], [], []).rows == []
    assert run([ff()], [], [], cfg=me.ValidationConfig(families=("NFP",))).rows == []


# ---- CLI / reports ----
def _write(tmp_path, ffs, mqs, frs):
    paths = {}
    for name, evs in (("ff", ffs), ("mql5", mqs), ("fred", frs)):
        p = tmp_path / f"{name}.parquet"
        events_to_dataframe(evs).to_parquet(p)
        paths[name] = p
    return ["--ff-events", str(paths["ff"]), "--mql5-events", str(paths["mql5"]), "--fred-events", str(paths["fred"]),
            "--report-dir", str(tmp_path / "rep")]


def test_cli_writes_csv_and_json_with_vintage_and_latest_columns(tmp_path, capsys):
    args = _write(tmp_path, [ff()], [mq(ts=RELEASE)], [official(value=0.3825), latest(value=0.3483)])
    assert cli.main(["--start", "2025-09-01", "--end", "2025-09-30", *args, "--strict"]) == 0
    out = capsys.readouterr().out
    assert "CPI_MOM:2025-08" in out and "ROUNDING_MATCH" in out
    js = json.loads((tmp_path / "rep/macro_validation_2025-09-01_2025-09-30.json").read_text())
    r = js["rows"][0]
    assert js["summary"] == {"MATCH": 1} and r["release_date"] == "2025-09-11" and r["reference_period"] == "2025-08-01"
    assert r["official_release_vintage_value"] == 0.3825 and r["official_latest_value"] == 0.3483
    assert "consensus" in js["meta"]["roles"]["forex_factory"]
    header = (tmp_path / "rep/macro_validation_2025-09-01_2025-09-30.csv").read_text().splitlines()[0].split(",")
    assert header == me.ROW_FIELDS


def test_cli_prints_exact_fetch_commands_for_missing_vintages(tmp_path, capsys):
    args = _write(tmp_path, [ff()], [mq(ts=RELEASE)], [])
    cli.main(["--start", "2025-09-01", "--end", "2025-09-30", *args])
    out = capsys.readouterr().out
    assert ("python3 scripts/fetch_fred_asof.py --event-family CPI_MOM --observation-start 2024-07-01 "
            "--observation-end 2025-08-31 --as-of 2025-09-11") in out


def test_cli_broker_timezone_flag(tmp_path, capsys):
    args = _write(tmp_path, [ff()], [mq(ts=None)], [official(value=0.4)])
    cli.main(["--start", "2025-09-01", "--end", "2025-09-30", *args, "--mql5-broker-timezone", "Europe/Helsinki"])
    js = json.loads((tmp_path / "rep/macro_validation_2025-09-01_2025-09-30.json").read_text())
    assert js["rows"][0]["timestamp_status"] == "MATCH" and js["meta"]["config"]["mql5_broker_timezone"] == "Europe/Helsinki"


def test_cli_strict_exits_nonzero_on_any_non_match(tmp_path):
    args = _write(tmp_path, [ff()], [], [])
    assert cli.main(["--start", "2025-09-01", "--end", "2025-09-30", *args, "--strict"]) == 1


def test_cli_rejects_bad_dates():
    assert cli.main(["--start", "2025-09-30", "--end", "2025-09-01"]) == 2
    assert cli.main(["--start", "nope", "--end", "2025-09-01"]) == 2
