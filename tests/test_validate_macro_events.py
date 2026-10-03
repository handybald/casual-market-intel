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


def ff(family="CPI_MOM", actual=0.4, forecast=0.3, prev=0.2, revised=None, ref=REF, ts=RELEASE, unit="PERCENT", tag="a",
       quality=None):
    return MacroEvent(event_id=f"ff:{family}:{tag}", event_family=family, indicator=indicator(family), reference_period=ref,
                      release_timestamp_utc=ts,
                      timestamp_quality=quality or (TimestampQuality.CONFIRMED if ts else TimestampQuality.TENTATIVE),
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


PRIOR_REF = dt.date(2025, 7, 1)
PRIOR_RELEASE = dt.date(2025, 8, 12)


def prior_data(m_events, prior_release=PRIOR_RELEASE, release_date=RELEASE_DATE):
    """Consistent prior-period data for each MQL5 event: the calendar's own earlier release of the same indicator
    (anchors the `previous` vintage date) plus ALFRED vintages of R-1 at the previous and at the current release."""
    out_ff, out_fred = [], []
    for e in m_events:
        unit = e.actual_unit if isinstance(e.actual_unit, str) else e.actual_unit.value
        series = SERIES.get(e.event_family, "X")
        out_ff.append(ff(e.event_family, actual=1.0, forecast=1.0, prev=1.0, ref=PRIOR_REF,
                         ts=dt.datetime.combine(prior_release, dt.time(12, 30), tzinfo=UTC), unit=unit, tag="prior"))
        if e.previous is not None:
            out_fred.append(official(e.event_family, e.previous, ref=PRIOR_REF, vintage=prior_release, unit=unit,
                                     series=series, tag="prev"))
        rev = e.revised_previous if e.revised_previous is not None else e.previous
        if rev is not None:
            out_fred.append(official(e.event_family, rev, ref=PRIOR_REF, vintage=release_date, unit=unit,
                                     series=series, tag="rev"))
    return out_ff, out_fred


def run(f=(), m=(), r=(), cfg=None, profiles=PROFILES, prior=True, rd=RELEASE_DATE):
    f, m, r = list(f), list(m), list(r)
    if prior:
        pf, pr = prior_data(m, release_date=rd)
        f, r = f + pf, r + pr
    return me.reconcile(f, m, r, START, END, MAPPING, cfg or me.ValidationConfig(), profiles)


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
    row = one(run([ff()], [mq(ts=RELEASE)], [], prior=False))
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
                   latest("NFP", -70, unit="THOUSANDS", series="PAYEMS")], rd=dt.date(2025, 9, 5)))
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
    pf, pr = prior_data(mqs)
    ffs, frs = list(ffs) + pf, list(frs) + pr
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


# =========================== PART 1: previous / revised_previous vs official prior-period vintages ===========================
NFP_REL = dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC)
NFP_RD = NFP_REL.date()
NFP_PRIOR_RD = dt.date(2025, 8, 1)          # July NFP's own release
JULY = dt.date(2025, 7, 1)


def nfp_case(prev=73, revised=79, off_prev=73, off_revised=79, extra_fred=(), prior_release_row=True, unit="THOUSANDS"):
    """September-2025-style NFP: previous 73K (July as first published Aug 1), revised_previous 79K (July as revised Sep 5)."""
    ffs = [ff("NFP", 22, 75, 73, revised=79, ts=NFP_REL, unit="THOUSANDS")]
    if prior_release_row:
        ffs.append(ff("NFP", 73, 110, 147, ref=JULY, ts=dt.datetime(2025, 8, 1, 12, 30, tzinfo=UTC), unit="THOUSANDS", tag="jul"))
    fred = [official("NFP", 22, vintage=NFP_RD, unit="THOUSANDS", series="PAYEMS", tag="aug"),
            *extra_fred]
    if off_prev is not None:
        fred.append(official("NFP", off_prev, ref=JULY, vintage=NFP_PRIOR_RD, unit="THOUSANDS", series="PAYEMS", tag="jul1"))
    if off_revised is not None:
        fred.append(official("NFP", off_revised, ref=JULY, vintage=NFP_RD, unit="THOUSANDS", series="PAYEMS", tag="jul2"))
    return run(ffs, [mq("NFP", 22, prev, revised=revised, ts=NFP_REL, unit=unit)], fred, prior=False, rd=NFP_RD)


def test_september_nfp_previous_and_revised_previous_validate_against_their_own_vintages():
    row = one(nfp_case())
    assert (row["mql5_previous"], row["mql5_revised_previous"]) == (73, 79)          # originals untouched
    assert row["previous_reference_period"] == "2025-07-01" and row["previous_prior_release_date"] == "2025-08-01"
    assert (row["previous_official_release_vintage"], row["previous_official_vintage_date"]) == (73, "2025-08-01")
    assert (row["revised_previous_official_release_vintage"], row["revised_previous_official_vintage_date"]) == (79, "2025-09-05")
    assert row["previous_validation_status"] == me.EXACT_MATCH and row["revised_previous_validation_status"] == me.EXACT_MATCH
    assert row["official_prior_period_revision"] == 6 and row["source_match_status"] == me.MATCH


def test_previous_is_not_validated_against_the_current_vintage():
    # official July was 73 at the Aug-1 vintage and 79 at the Sep-5 vintage; MQL5 previous=73 must match the FIRST
    row = one(nfp_case())
    assert row["previous_validation_status"] == me.EXACT_MATCH
    wrong = one(nfp_case(prev=79))                 # calendar `previous` equal to the revised value would be wrong
    assert wrong["previous_validation_status"] == me.VALUE_MISMATCH and wrong["source_match_status"] == me.VALUE_MISMATCH


def test_revised_previous_mismatch_against_official_revision():
    row = one(nfp_case(revised=80))
    assert row["revised_previous_validation_status"] == me.VALUE_MISMATCH and row["previous_validation_status"] == me.EXACT_MATCH
    assert row["mql5_revised_previous"] == 80 and "MQL5 revised_previous 80 vs official vintage 2025-09-05" in row["issues"]


def test_revised_previous_equal_to_previous_is_still_validated_separately():
    row = one(run([ff("CPI_MOM", 0.4, 0.3, 0.2, ts=RELEASE)], [mq("CPI_MOM", 0.4, 0.2, revised=0.2, ts=RELEASE)],
                  [official(value=0.4)], prior=True))
    assert row["mql5_previous_differs_from_revised"] is False
    assert row["previous_validation_status"] == row["revised_previous_validation_status"] == me.EXACT_MATCH


def test_rounding_applies_to_prior_period_vintages_too():
    row = one(run([ff()], [mq(actual=0.4, prev=0.2, revised=0.3, ts=RELEASE)], [official(value=0.4),
                official(ref=PRIOR_REF, value=0.2449, vintage=PRIOR_RELEASE), official(ref=PRIOR_REF, value=0.2751, vintage=RELEASE_DATE)],
                prior=False)) if False else None
    rows = [ff(), ff(ref=PRIOR_REF, ts=dt.datetime.combine(PRIOR_RELEASE, dt.time(12, 30), tzinfo=UTC), tag="p")]
    res = run(rows, [mq(actual=0.4, prev=0.2, revised=0.3, ts=RELEASE)],
              [official(value=0.4), official(ref=PRIOR_REF, value=0.2449, vintage=PRIOR_RELEASE, tag="p1"),
               official(ref=PRIOR_REF, value=0.2751, vintage=RELEASE_DATE, tag="p2")], prior=False)
    row = one(res)
    assert row["previous_validation_status"] == me.ROUNDING_MATCH and row["revised_previous_validation_status"] == me.ROUNDING_MATCH


def test_revised_previous_absent_is_not_applicable_but_unreported_revision_is_flagged():
    row = one(nfp_case(revised=None))
    assert row["revised_previous_validation_status"] == "NOT_APPLICABLE" and row["mql5_revised_previous"] is None
    assert "PRIOR_REVISION_NOT_REPORTED" in row["issues"] and row["previous_validation_status"] == me.EXACT_MATCH


def test_prior_vintage_unavailable():
    row = one(nfp_case(off_prev=None))
    assert row["previous_validation_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    assert row["revised_previous_validation_status"] == me.EXACT_MATCH and row["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    both = one(nfp_case(off_prev=None, off_revised=None))
    assert both["revised_previous_validation_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE


def test_prior_release_date_unknown_cannot_anchor_previous_vintage():
    row = one(nfp_case(prior_release_row=False))
    assert row["previous_validation_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE and "prior release date unknown" in row["issues"]
    assert row["revised_previous_validation_status"] == me.EXACT_MATCH        # anchored on the current release date


def test_no_lookahead_official_revision_for_the_prior_period():
    later = official("NFP", 91, ref=JULY, vintage=dt.date(2025, 10, 3), unit="THOUSANDS", series="PAYEMS", tag="later")
    latest_july = latest("NFP", 88, ref=JULY, unit="THOUSANDS", series="PAYEMS", tag="l")
    row = one(nfp_case(extra_fred=[later, latest_july]))
    assert row["revised_previous_official_release_vintage"] == 79 and row["revised_previous_official_vintage_date"] == "2025-09-05"
    assert row["revised_previous_validation_status"] == me.EXACT_MATCH        # 91 and 88 were not known at the release
    only_late = one(nfp_case(off_revised=None, extra_fred=[later, latest_july]))
    assert only_late["revised_previous_validation_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
    assert only_late["revised_previous_official_release_vintage"] is None and "later than the release" in only_late["issues"]


def test_prior_period_unit_unverified():
    row = one(nfp_case(unit="UNKNOWN"))
    assert row["previous_validation_status"] == me.UNIT_UNVERIFIED and row["revised_previous_validation_status"] == me.UNIT_UNVERIFIED


# =========================== PART 2: broker timezone verification ===========================
def tz_events(pairs, tz_ts=None):
    """pairs: [(family, ff_utc, mql5_server_local_naive)] -> (ff events, mql5 events)"""
    ffs, mqs = [], []
    for i, (fam, utc, local) in enumerate(pairs):
        ffs.append(ff(fam, ts=utc, ref=dt.date(utc.year, utc.month, 1), tag=f"t{i}"))
        mqs.append(mq(fam, ts=None, server=local, ref=dt.date(utc.year, utc.month, 1), tag=f"t{i}"))
    return ffs, mqs


def tz_run(pairs, tz, start=dt.date(2025, 1, 1), end=dt.date(2025, 12, 31), min_rel=2):
    ffs, mqs = tz_events(pairs)
    cfg = me.ValidationConfig(mql5_broker_timezone=tz, min_tz_evidence_releases=min_rel)
    return me.reconcile(ffs, mqs, [], start, end, MAPPING, cfg, PROFILES)


SUMMER = [("NFP", dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC), dt.datetime(2025, 9, 5, 15, 30)),        # EDT, EEST(+3)
          ("CPI_MOM", dt.datetime(2025, 9, 11, 12, 30, tzinfo=UTC), dt.datetime(2025, 9, 11, 15, 30))]
WINTER = [("NFP", dt.datetime(2025, 12, 5, 13, 30, tzinfo=UTC), dt.datetime(2025, 12, 5, 15, 30)),      # EST, EET(+2)
          ("CPI_MOM", dt.datetime(2025, 12, 10, 13, 30, tzinfo=UTC), dt.datetime(2025, 12, 10, 15, 30))]
# 2025: EU switches 10-26, US switches 11-02 -> between those dates ET is EDT (UTC-4) while Helsinki is already +2
TRANSITION = [("NFP", dt.datetime(2025, 10, 24, 12, 30, tzinfo=UTC), dt.datetime(2025, 10, 24, 15, 30)),   # both summer: +3
              ("CPI_MOM", dt.datetime(2025, 10, 29, 12, 30, tzinfo=UTC), dt.datetime(2025, 10, 29, 14, 30)),  # gap week: +2
              ("UNEMPLOYMENT_RATE", dt.datetime(2025, 11, 7, 13, 30, tzinfo=UTC), dt.datetime(2025, 11, 7, 15, 30))]  # both winter: +2


def test_summer_utc_plus_3_helsinki_matches():
    res = tz_run(SUMMER, "Europe/Helsinki")
    tz = res.timezone_validation
    assert tz["status"] == "MATCH" and tz["distinct_trusted_releases"] == 2 and tz["max_abs_delta_seconds"] == 0
    # Two summer releases fit Helsinki, but ALSO a fixed +3: one UTC offset cannot tell a DST zone from a fixed one
    assert tz["observed_utc_offsets_hours"] == [3.0] and tz["dst_regimes_covered"] is False
    assert tz["evidence_sufficient"] is False and tz["mql5_timestamps_trusted"] is False
    first = tz["per_release"][0]
    assert first["mql5_raw_timestamp"] == "2025-09-05T15:30:00" and first["mql5_converted_utc"] == "2025-09-05T12:30:00+00:00"
    assert first["candidate_broker_timezone"] == "Europe/Helsinki" and first["ff_confirmed_utc"] == "2025-09-05T12:30:00+00:00"
    assert all(r["timezone_validation_status"] == "MATCH" and r["timestamp_delta_seconds"] == 0 for r in res.rows)


def test_winter_utc_plus_2_helsinki_matches():
    tz = tz_run(WINTER, "Europe/Helsinki").timezone_validation
    assert tz["status"] == "MATCH" and tz["max_abs_delta_seconds"] == 0
    assert tz_run(WINTER, "Etc/GMT-3").timezone_validation["status"] == "MISMATCH"     # a +3 zone is wrong in winter


def test_dst_transition_weeks_including_the_us_eu_gap_week():
    tz = tz_run(TRANSITION, "Europe/Helsinki").timezone_validation
    assert tz["status"] == "MATCH" and tz["distinct_trusted_releases"] == 3 and tz["max_abs_delta_seconds"] == 0
    # a fixed +3 offset matches only the first event; a fixed +2 offset only the last two
    plus3 = tz_run(TRANSITION, "Etc/GMT-3").timezone_validation
    assert plus3["status"] == "MISMATCH" and plus3["releases_matching"] == 1 and plus3["releases_mismatching"] == 2
    assert plus3["mql5_timestamps_trusted"] is False
    plus2 = tz_run(TRANSITION, "Etc/GMT-2").timezone_validation
    assert plus2["releases_matching"] == 2 and plus2["releases_mismatching"] == 1


def test_us_dst_shifts_the_release_but_not_the_broker_offset_relation():
    # Same 08:30 ET release: 12:30Z in summer and 13:30Z in winter; a Helsinki-time server shows 15:30 both times.
    both = tz_run(SUMMER + WINTER, "Europe/Helsinki").timezone_validation
    assert both["status"] == "MATCH" and both["distinct_trusted_releases"] == 4


def test_wrong_timezone_is_a_mismatch_with_per_event_deltas():
    res = tz_run(SUMMER, "UTC")
    tz = res.timezone_validation
    assert tz["status"] == "MISMATCH" and tz["releases_mismatching"] == 2 and tz["mql5_timestamps_trusted"] is False
    assert tz["per_release"][0]["delta_seconds"] == 3 * 3600
    assert all(r["source_match_status"] == me.TIMESTAMP_MISMATCH or r["source_match_status"] == me.OFFICIAL_VINTAGE_UNAVAILABLE
               or r["timestamp_status"] == "MISMATCH" for r in res.rows)


def test_missing_trusted_ff_timestamp_is_unverifiable():
    ffs, mqs = tz_events(SUMMER)
    ffs = [e.model_copy(update={"timestamp_quality": TimestampQuality.ASSUMED}) for e in ffs]      # not CONFIRMED
    cfg = me.ValidationConfig(mql5_broker_timezone="Europe/Helsinki")
    res = me.reconcile(ffs, mqs, [], dt.date(2025, 9, 1), dt.date(2025, 9, 30), MAPPING, cfg, PROFILES)
    assert res.timezone_validation["status"] == "UNVERIFIABLE" and res.timezone_validation["mql5_timestamps_trusted"] is False
    assert all(r["timestamp_status"] == "UNVERIFIABLE" and r["trusted_release_timestamp_utc"] is None for r in res.rows)
    none_ts = tz_run([], "Europe/Helsinki")
    assert none_ts.timezone_validation["status"] == "UNVERIFIABLE"


def test_unresolved_mql5_timestamp_is_unverifiable_and_never_trusted():
    res = tz_run(SUMMER, None)
    assert res.timezone_validation["status"] == "UNVERIFIABLE" and res.timezone_validation["mql5_timestamps_trusted"] is False
    row = res.rows[0]
    assert row["mql5_converted_utc"] is None and row["mql5_implied_server_utc_offset_hours"] == 3.0
    assert row["trusted_release_timestamp_basis"] == "FF_CONFIRMED"             # FF still trusted; MQL5 is not


def test_mql5_time_is_trusted_only_under_a_validated_timezone_and_only_without_ff():
    ffs, mqs = tz_events(SUMMER + WINTER)
    only_mql5 = mq("AVG_HOURLY_EARNINGS_YOY", ts=None, server=dt.datetime(2025, 9, 5, 15, 30), tag="only")
    win = (dt.date(2025, 9, 1), dt.date(2025, 9, 30))
    cfg = me.ValidationConfig(mql5_broker_timezone="Europe/Helsinki")
    # evidence spans summer AND winter offsets -> Helsinki is identifiable and validated
    res = me.reconcile(ffs, mqs + [only_mql5], [], *win, MAPPING, cfg, PROFILES,
                       timezone_evidence=tz_run(SUMMER + WINTER, "Europe/Helsinki").timezone_validation)
    by = {r["event_family"]: r for r in res.rows}
    assert by["NFP"]["trusted_release_timestamp_basis"] == "FF_CONFIRMED"            # FF always wins
    yoy = by["AVG_HOURLY_EARNINGS_YOY"]
    assert yoy["trusted_release_timestamp_utc"] == "2025-09-05T12:30:00+00:00"
    assert yoy["trusted_release_timestamp_basis"] == "MQL5_VALIDATED_BROKER_TZ:Europe/Helsinki"
    # the window's own evidence (summer only) is NOT enough for a DST zone
    own = me.reconcile(ffs, mqs + [only_mql5], [], *win, MAPPING, cfg, PROFILES)
    assert {r["event_family"]: r for r in own.rows}["AVG_HOURLY_EARNINGS_YOY"]["trusted_release_timestamp_utc"] is None
    # a wrong timezone never yields trusted MQL5 times, even with its own evidence window
    wrong = me.ValidationConfig(mql5_broker_timezone="UTC")
    bad = me.reconcile(ffs, mqs + [only_mql5], [], *win, MAPPING, wrong, PROFILES,
                       timezone_evidence=tz_run(SUMMER + WINTER, "UTC").timezone_validation)
    assert {r["event_family"]: r for r in bad.rows}["AVG_HOURLY_EARNINGS_YOY"]["trusted_release_timestamp_utc"] is None
    # evidence computed for a DIFFERENT candidate zone cannot be borrowed
    other = me.reconcile(ffs, mqs + [only_mql5], [], *win, MAPPING, cfg, PROFILES,
                         timezone_evidence=tz_run(SUMMER + WINTER, "Etc/GMT-3").timezone_validation)
    assert {r["event_family"]: r for r in other.rows}["AVG_HOURLY_EARNINGS_YOY"]["trusted_release_timestamp_utc"] is None


def test_fixed_offset_zone_needs_no_dst_coverage_but_helsinki_is_falsified_by_winter_data():
    """The real 2019-2020 finding: an MQL5 export that is a constant UTC+3 fits `Etc/GMT-3` in summer AND winter, while
    Europe/Helsinki (+2 in winter) is off by one hour on every winter release."""
    fixed = tz_run(SUMMER, "Etc/GMT-3").timezone_validation
    assert fixed["status"] == "MATCH" and fixed["dst_regimes_covered"] is True and fixed["mql5_timestamps_trusted"] is True
    winter_plus3 = [("NFP", dt.datetime(2025, 12, 5, 13, 30, tzinfo=UTC), dt.datetime(2025, 12, 5, 16, 30)),
                    ("CPI_MOM", dt.datetime(2025, 12, 10, 13, 30, tzinfo=UTC), dt.datetime(2025, 12, 10, 16, 30))]
    data = SUMMER + winter_plus3
    assert tz_run(data, "Etc/GMT-3").timezone_validation["status"] == "MATCH"
    helsinki = tz_run(data, "Europe/Helsinki").timezone_validation
    assert helsinki["status"] == "MISMATCH" and helsinki["releases_mismatching"] == 2
    assert all(abs(x["delta_seconds"]) == 3600 for x in helsinki["per_release"] if x["status"] == "MISMATCH")
    assert helsinki["mql5_timestamps_trusted"] is False


def test_dst_zone_with_both_regimes_in_evidence_is_sufficient():
    both = tz_run(SUMMER + WINTER, "Europe/Helsinki").timezone_validation
    assert both["observed_utc_offsets_hours"] == [2.0, 3.0] and both["dst_regimes_covered"] is True
    assert both["evidence_sufficient"] is True and both["mql5_timestamps_trusted"] is True


def test_convert_broker_local_to_utc_dst_edges():
    conv = me.convert_broker_local_to_utc
    assert conv(dt.datetime(2025, 7, 1, 15, 30), "Europe/Helsinki")[0] == dt.datetime(2025, 7, 1, 12, 30, tzinfo=UTC)
    assert conv(dt.datetime(2025, 1, 8, 15, 30), "Europe/Helsinki")[0] == dt.datetime(2025, 1, 8, 13, 30, tzinfo=UTC)
    amb_utc, note = conv(dt.datetime(2025, 10, 26, 3, 30), "Europe/Helsinki", dt.datetime(2025, 10, 26, 0, 30, tzinfo=UTC))
    assert "ambiguous" in note and amb_utc == dt.datetime(2025, 10, 26, 0, 30, tzinfo=UTC)        # closest to the FF time
    amb_utc2, _ = conv(dt.datetime(2025, 10, 26, 3, 30), "Europe/Helsinki", dt.datetime(2025, 10, 26, 1, 30, tzinfo=UTC))
    assert amb_utc2 == dt.datetime(2025, 10, 26, 1, 30, tzinfo=UTC)
    _, gap = conv(dt.datetime(2025, 3, 30, 3, 30), "Europe/Helsinki")
    assert "nonexistent" in gap


def test_cli_prints_timezone_validation_and_supplied_zone(tmp_path, capsys):
    ffs, mqs = tz_events(SUMMER)
    args = _write(tmp_path, ffs, mqs, [])
    cli.main(["--start", "2025-09-01", "--end", "2025-09-30", *args, "--mql5-broker-timezone", "Europe/Helsinki"])
    out = capsys.readouterr().out
    assert "Broker timezone validation" in out and "Europe/Helsinki" in out and "MATCH" in out
    js = json.loads((tmp_path / "rep/macro_validation_2025-09-01_2025-09-30.json").read_text())
    assert js["timezone_validation"]["status"] == "MATCH"


def test_tiny_official_prior_period_drift_is_not_reported_as_an_unreported_revision():
    # 0.3304 -> 0.3305 is invisible at the calendars' 1-decimal precision; 0.33 -> 0.36 is not
    def case(rev):
        ffs = [ff(), ff(ref=PRIOR_REF, ts=dt.datetime.combine(PRIOR_RELEASE, dt.time(12, 30), tzinfo=UTC), tag="p")]
        return one(run(ffs, [mq(actual=0.4, prev=0.3, revised=None, ts=RELEASE)],
                       [official(value=0.4), official(ref=PRIOR_REF, value=0.3304, vintage=PRIOR_RELEASE, tag="p1"),
                        official(ref=PRIOR_REF, value=rev, vintage=RELEASE_DATE, tag="p2")], prior=False))
    assert "PRIOR_REVISION_NOT_REPORTED" not in case(0.3305)["issues"]
    assert "PRIOR_REVISION_NOT_REPORTED" in case(0.3601)["issues"]
