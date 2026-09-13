"""Regression tests for scripts/import_forex_factory.py.

Confirms the behaviors specified for the historical-only Forex Factory
importer, using synthetic (not real-captured, for test speed/
hermeticity) but structurally faithful HTML fixtures -- verified against
a real user-saved page before being distilled here (see
test_normalize_forex_factory_structured.py's module docstring for the
same note on the structured-data shape).

1. Coverage is computed from the page's own rendered CALENDAR DAYS,
   never the filename/directory -- and a month a file only PARTIALLY
   covers is never recorded as fully "complete".
2. A page that looks like a bot-block/challenge page is refused.
3. Two files whose partial coverage of the same boundary month is
   complementary combine, via the manifest's interval-aware
   coverage_gaps, into full recognized coverage.
4. Recursive directory discovery (any subdirectory depth/layout).
5. `timezone_name` extracted from the page and used for conversions
   (never the page's own numeric UTC-offset field).
6. Checksum-based skip-unchanged, and --force to override it.
7. USD filtering, mapped/unmapped event counting and reporting.
8. Corrupted HTML never crashes the whole batch.
9. Re-importing the same/overlapping content never duplicates events.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from src.data.config import AppConfig
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest

import import_forex_factory as importer  # noqa: E402


def _event(event_id, ebase_id, name, dateline, time_label, date_str, currency="USD", revision="", time_masked=False):
    return {
        "id": event_id, "ebaseId": ebase_id, "name": name, "prefixedName": f"US {name}",
        "notice": "", "dateline": dateline, "country": "US", "currency": currency,
        "impactClass": "icon--ff-impact-red", "impactTitle": "High Impact Expected",
        "timeLabel": time_label, "timeMasked": time_masked,
        "actual": "1.0%", "forecast": "1.1%", "previous": "0.9%", "revision": revision,
        "date": date_str,
    }


def _day(date_label, dateline, events=None):
    return {"date": date_label, "dateline": dateline, "add": "", "events": events or []}


def _dateline(y, m, d, hour=0, minute=0, tz="America/New_York"):
    from zoneinfo import ZoneInfo
    local = dt.datetime(y, m, d, hour, minute, tzinfo=ZoneInfo(tz))
    return int(local.timestamp())


def _page(days, timezone_name="America/New_York", include_ff_settings=True):
    # Padded past _cheap_sanity_check's 500-byte "suspiciously short"
    # floor -- a tiny synthetic fixture is shorter than any real captured
    # page, which is exactly what that check exists to catch; pad here
    # rather than weaken the real check.
    padding = "<!-- " + ("padding " * 80) + " -->"
    ff_settings = (
        "<script>window.FF = {country_code: 'TR', timezone: '-4'.replace('+', ''), "
        f"timezone_name: '{timezone_name}', display_time: '7:45am'}}</script>"
        if include_ff_settings else ""
    )
    return (
        "<html><head><title>Calendar | Forex Factory</title></head><body>"
        + padding + ff_settings
        + "<script>if (typeof window.calendarComponentStates === 'undefined') { window.calendarComponentStates = {} }\n"
        "window.calendarComponentStates[1] = {\n"
        f"days: {json.dumps(days)},\n"
        "time: '7:45am', upNext: 'sep13.2026'\n"
        "}</script></body></html>"
    )


def make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2016-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"),
            "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"),
            "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "forex_factory": {
                "raw_dir": str(tmp_path / "raw" / "forex_factory"),
                "base_url": "https://www.forexfactory.com/calendar",
                "max_retries": 1,
                "request_delay_seconds": 0,
                "display_timezone": "America/New_York",
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


@pytest.fixture
def event_mapping():
    return load_event_mapping()


def _all_january_days(year=2016, tz="America/New_York"):
    days = []
    for d in range(1, 32):
        events = []
        if d == 8:
            events = [_event(100 + d, 66, "Non-Farm Employment Change", _dateline(year, 1, d, 8, 30, tz), "8:30am", f"Jan {d}, {year}")]
        days.append(_day(f"Day <span>Jan {d}</span>", _dateline(year, 1, d, tz=tz), events))
    return days


def _import_one(config, manifest, event_mapping, path, today=dt.date(2026, 1, 1), **kwargs):
    log = importer._load_import_log(config)
    result = importer.import_file(config, manifest, event_mapping, log, path, today, **kwargs)
    if result.status == "imported" and result.checksum:
        log[result.checksum] = {"path": str(path)}
    importer._save_import_log(config, log)
    return result


# =====================================================================
# 1. Coverage from event dates (day-level), never the filename/directory.
# =====================================================================

def test_full_month_recorded_with_full_month_boundaries(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "misleadingly_named_totally_different_range.html"
    html_path.write_text(_page(_all_january_days()), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "imported"
    assert manifest.is_complete("forex_factory", "ALL", "2016-01-01", "2016-01-31")


def test_partial_month_never_marked_complete_at_full_month_boundary(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    days = [_day("Tue <span>Mar 1</span>", _dateline(2016, 3, 1)), _day("Wed <span>Mar 2</span>", _dateline(2016, 3, 2))]
    html_path = tmp_path / "ff_2016_Q1.html"  # filename LIES -- claims a full quarter
    html_path.write_text(_page(days), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "imported"
    assert not manifest.is_complete("forex_factory", "ALL", "2016-03-01", "2016-03-31")
    assert manifest.is_complete("forex_factory", "ALL", "2016-03-01", "2016-03-02")


def test_non_contiguous_internal_gap_refused_entirely(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    days = [_day("Tue <span>Mar 1</span>", _dateline(2016, 3, 1)), _day("Thu <span>Mar 10</span>", _dateline(2016, 3, 10))]
    html_path = tmp_path / "gap.html"
    html_path.write_text(_page(days), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "refused"
    assert not manifest.is_complete("forex_factory", "ALL", "2016-03-01", "2016-03-10")


def test_coverage_ignores_filename_and_directory_entirely(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    fake_year_dir = tmp_path / "1999"  # directory name LIES too
    fake_year_dir.mkdir()
    html_path = fake_year_dir / "ff_1999_Q4.html"
    html_path.write_text(_page(_all_january_days(year=2016)), encoding="utf-8")

    _import_one(config, manifest, event_mapping, html_path)
    assert manifest.is_complete("forex_factory", "ALL", "2016-01-01", "2016-01-31")
    assert not manifest.is_complete("forex_factory", "ALL", "1999-10-01", "1999-12-31")


# =====================================================================
# 2. Bot-block/challenge pages are refused.
# =====================================================================

def test_challenge_page_refused_not_imported(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "accidentally_saved_challenge.html"
    html_path.write_text(
        "<html><head><title>Just a moment...</title></head><body>"
        + ("Checking your browser before accessing forexfactory.com. " * 30)
        + "</body></html>",
        encoding="utf-8",
    )

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "refused"
    assert manifest.entries_for("forex_factory") == []


def test_page_without_structured_data_and_no_year_hint_refused(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "old_page_no_structured_data.html"
    html_path.write_text(
        "<html><body>" + ("<table class='calendar'><tr class='calendar__row'></tr></table> " * 20) + "</body></html>",
        encoding="utf-8",
    )

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "refused"


# =====================================================================
# 3. Complementary partial imports combine into full coverage.
# =====================================================================

def test_two_partial_imports_of_same_month_combine_to_full_coverage(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    first_half = [_day(f"Day <span>Mar {d}</span>", _dateline(2016, 3, d)) for d in range(1, 16)]
    second_half = [_day(f"Day <span>Mar {d}</span>", _dateline(2016, 3, d)) for d in range(16, 32)]
    path1 = tmp_path / "part1.html"
    path1.write_text(_page(first_half), encoding="utf-8")
    path2 = tmp_path / "part2.html"
    path2.write_text(_page(second_half), encoding="utf-8")

    _import_one(config, manifest, event_mapping, path1)
    _import_one(config, manifest, event_mapping, path2)

    assert not manifest.is_complete("forex_factory", "ALL", "2016-03-01", "2016-03-31")
    gaps = manifest.coverage_gaps("forex_factory", "ALL", dt.date(2016, 3, 1), dt.date(2016, 3, 31))
    assert gaps == []
    assert manifest.completion_watermark("forex_factory", "ALL", dt.date(2016, 3, 1)) == dt.date(2016, 3, 31)


# =====================================================================
# 4. Recursive directory discovery.
# =====================================================================

def test_recursive_discovery_finds_nested_files(tmp_path):
    (tmp_path / "2016" / "q1").mkdir(parents=True)
    (tmp_path / "2017").mkdir()
    f1 = tmp_path / "2016" / "q1" / "a.html"
    f1.write_text("x", encoding="utf-8")
    f2 = tmp_path / "2017" / "b.htm"
    f2.write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")  # must be ignored

    found = importer.discover_files(tmp_path)
    assert found == sorted([f1, f2])


# =====================================================================
# 5. timezone_name extraction and usage (never the numeric offset).
# =====================================================================

def test_timezone_name_extracted_and_used_not_numeric_offset(tmp_path, event_mapping):
    """A page saved with timezone_name != the config default must still
    convert correctly using the PAGE's own declared zone."""
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    # Use a different-but-real zone to prove the page's own value is used,
    # not the config's "America/New_York" default.
    days = _all_january_days(tz="America/Chicago")
    html_path = tmp_path / "chicago.html"
    html_path.write_text(_page(days, timezone_name="America/Chicago"), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "imported"
    # If the page's own Chicago timezone were ignored in favor of the
    # config's NY default, day-level dateline->date conversion would be
    # off by an hour's worth of boundary risk for midnight-anchored
    # datelines -- confirm the full month still came through intact.
    assert manifest.is_complete("forex_factory", "ALL", "2016-01-01", "2016-01-31")

    import pandas as pd
    df = pd.read_parquet(config.interim_root / "macro" / "forex_factory_events.parquet")
    assert len(df) == 1
    assert df.iloc[0]["timestamp_quality"] == "CONFIRMED"
    # 8:30am America/Chicago on Jan 8 2016 (CST, UTC-6) == 14:30 UTC.
    assert df.iloc[0]["release_timestamp_utc"] == pd.Timestamp("2016-01-08 14:30:00", tz="UTC")


def test_missing_timezone_name_falls_back_to_config_default(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "no_ff_settings.html"
    html_path.write_text(_page(_all_january_days(), include_ff_settings=False), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "imported"
    assert manifest.is_complete("forex_factory", "ALL", "2016-01-01", "2016-01-31")


# =====================================================================
# 6. Checksum-based skip-unchanged, and --force.
# =====================================================================

def test_reimporting_unchanged_file_is_skipped(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "a.html"
    html_path.write_text(_page(_all_january_days()), encoding="utf-8")

    r1 = _import_one(config, manifest, event_mapping, html_path)
    assert r1.status == "imported"
    r2 = _import_one(config, manifest, event_mapping, html_path)
    assert r2.status == "skipped_unchanged"


def test_force_reprocesses_unchanged_file(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "a.html"
    html_path.write_text(_page(_all_january_days()), encoding="utf-8")

    _import_one(config, manifest, event_mapping, html_path)
    r2 = _import_one(config, manifest, event_mapping, html_path, force=True)
    assert r2.status == "imported"


# =====================================================================
# 7. USD filtering, mapped/unmapped counting.
# =====================================================================

def test_usd_and_mapped_unmapped_counts(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    events = [
        _event(1, 66, "Non-Farm Employment Change", _dateline(2016, 1, 8, 8, 30), "8:30am", "Jan 8, 2016", currency="USD"),
        _event(2, 999, "Some Totally Unmapped USD Release", _dateline(2016, 1, 8, 9, 0), "9:00am", "Jan 8, 2016", currency="USD"),
        _event(3, 66, "Non-Farm Employment Change", _dateline(2016, 1, 8, 8, 30), "8:30am", "Jan 8, 2016", currency="EUR"),
    ]
    days = [_day("Fri <span>Jan 8</span>", _dateline(2016, 1, 8), events)]
    html_path = tmp_path / "a.html"
    html_path.write_text(_page(days), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "imported"
    assert result.stats["event_count"] == 3
    assert result.stats["usd_event_count"] == 2
    assert result.stats["mapped_usd_event_count"] == 1
    assert result.stats["unmapped_usd_event_count"] == 1
    assert result.unmapped_names == ["Some Totally Unmapped USD Release"]


def test_unmapped_report_file_written(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    events = [_event(2, 999, "Some Totally Unmapped USD Release", _dateline(2016, 1, 8, 9, 0), "9:00am", "Jan 8, 2016", currency="USD")]
    days = [_day("Fri <span>Jan 8</span>", _dateline(2016, 1, 8), events)]
    html_path = tmp_path / "a.html"
    html_path.write_text(_page(days), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path)
    importer._write_unmapped_report(config, [result])
    report_path = config.manifest_path.parent / "forex_factory_unmapped_events.txt"
    assert report_path.exists()
    assert "Some Totally Unmapped USD Release" in report_path.read_text(encoding="utf-8")


# =====================================================================
# 8. Corrupted HTML never crashes the batch.
# =====================================================================

def test_corrupted_html_refused_not_crashed(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "corrupt.html"
    html_path.write_bytes(b"\xff\xfe\x00\x01<html>" + b"not really calendar data" * 30)

    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "refused"  # never raises


def test_malformed_structured_data_does_not_crash(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "malformed.html"
    padding = "<!-- " + ("padding " * 80) + " -->"
    html_path.write_text(
        "<html><body>" + padding
        + "<script>window.calendarComponentStates[1] = {\ndays: [{\"date\": \"Jan 1, 2016\", not_valid_json here,\n}</script></body></html>",
        encoding="utf-8",
    )
    result = _import_one(config, manifest, event_mapping, html_path)
    assert result.status == "refused"  # never raises


# =====================================================================
# 9. Preserve original previous + revision separately (importer-level
# confirmation that normalized output keeps both, matching
# test_normalize_forex_factory_structured.py's more focused unit test).
# =====================================================================

def test_previous_and_revision_both_preserved_via_import(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    ev = _event(1, 66, "Non-Farm Employment Change", _dateline(2016, 2, 5, 8, 30), "8:30am", "Feb 5, 2016", revision="252K")
    ev["previous"] = "211K"
    ev["actual"] = "292K"
    ev["forecast"] = "203K"
    days = [_day("Fri <span>Feb 5</span>", _dateline(2016, 2, 5), [ev])]
    html_path = tmp_path / "a.html"
    html_path.write_text(_page(days), encoding="utf-8")

    _import_one(config, manifest, event_mapping, html_path)

    import pandas as pd
    df = pd.read_parquet(config.interim_root / "macro" / "forex_factory_events.parquet")
    row = df.iloc[0]
    assert row["previous"] == 211.0
    assert row["revised_previous"] == 252.0
    assert row["actual"] == 292.0
    assert row["provider_forecast"] == 203.0
    assert row["forecast_source"] == "FOREX_FACTORY"


# =====================================================================
# --start/--end clipping
# =====================================================================

def test_start_end_clips_recorded_coverage(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    html_path = tmp_path / "a.html"
    html_path.write_text(_page(_all_january_days()), encoding="utf-8")

    result = _import_one(config, manifest, event_mapping, html_path, range_start=dt.date(2016, 1, 10), range_end=dt.date(2016, 1, 20))
    assert result.status == "imported"
    assert not manifest.is_complete("forex_factory", "ALL", "2016-01-01", "2016-01-31")
    assert manifest.is_complete("forex_factory", "ALL", "2016-01-10", "2016-01-20")
