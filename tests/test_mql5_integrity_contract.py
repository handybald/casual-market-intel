"""Regression tests (fifth review, P1, issue #1): the window-log "OK
and count > 0" check (fourth review's fix) proved only that SOME data
exists for a window, not that the exported window's data is intact.
None of these were detectable by it:
  - the CSV truncated back down to just its header,
  - a PARTIAL truncation that still leaves some (but not all) rows,
  - an in-place edit that changes a value without changing row count.

The fix (see src/data/fetch/mql5.py's module docstring, "WINDOW
INTEGRITY CONTRACT"): the exporter records a CONTENT DIGEST over the
CANONICAL (deduplicated-by-value_id) row set it exported for a window;
ingestion recomputes the same digest from the CSV's CURRENT content and
only certifies completion on an exact (count, digest) match.

These tests exercise the REAL, production `ingest_mql5_calendar` /
`read_window_log` / `window_content_digest` / `_read_raw_csv_rows`
against realistic evidence -- constructed either directly or via
`tests/_mql5_exporter_sim.py`'s `SimulatedMql5Exporter` (a Python
mirror of the redesigned .mq5 file, since no MetaTrader compiler/
runtime is available in this environment; see that module's own
docstring and the final report's fixture-tested vs. simulated
distinction).
"""
import datetime as dt
import shutil
from pathlib import Path

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch.mql5 import (
    MQL5_EXPORT_SCHEMA_VERSION,
    ingest_mql5_calendar,
    window_content_digest,
)
from tests._mql5_exporter_sim import SimulatedMql5Exporter

CSV_HEADER = (
    "value_id,event_id,event_name,country_code,currency_code,importance,event_time,"
    "period,unit,multiplier,actual_value,forecast_value,prev_value,revised_prev_value,"
    "revision,source_timezone"
)


def make_config(tmp_path, csv_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2016-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "mql5": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "mql5"),
                      "input_csv": str(csv_path), "broker_timezone": None},
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _row(value_id, event_time, actual="216.000000"):
    return {
        "value_id": value_id, "event_id": "100", "event_name": "NFP", "country_code": "US",
        "currency_code": "USD", "importance": "HIGH", "event_time": event_time, "period": "2023.12.01",
        "unit": "JOB", "multiplier": "THOUSANDS", "actual_value": actual, "forecast_value": "",
        "prev_value": "", "revised_prev_value": "", "revision": "0", "source_timezone": "SERVER",
    }


def _write_csv(path: Path, rows) -> None:
    lines = [CSV_HEADER] + [",".join(r[c] for c in [
        "value_id", "event_id", "event_name", "country_code", "currency_code", "importance",
        "event_time", "period", "unit", "multiplier", "actual_value", "forecast_value",
        "prev_value", "revised_prev_value", "revision", "source_timezone",
    ]) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_ok_log_entry(csv_path: Path, win_start: dt.date, win_end_inclusive: dt.date, rows_at_export_time) -> None:
    """Writes a genuinely correct "OK" evidence row for the given rows
    -- exactly what a real successful exporter run would have recorded
    at that moment."""
    count, digest = window_content_digest(rows_at_export_time)
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    win_end_exclusive = win_end_inclusive + dt.timedelta(days=1)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{win_start:%Y.%m.%d} 00:00:00,{win_end_exclusive:%Y.%m.%d} 00:00:00,US,USD,"
            f"{MQL5_EXPORT_SCHEMA_VERSION},OK,{count},1,2024.01.01 00:00:00,{digest}\n"
        )


def _old_naive_would_certify(log_says_ok: bool, current_row_count: int) -> bool:
    """The PRE-fifth-review check this replaces: `log_says_ok and count
    > 0`. Used only as a documented point of comparison (not executed
    production code) to show which of these scenarios it would have
    gotten wrong."""
    return log_says_ok and current_row_count > 0


# -- 1: log claims four rows; only one remains --------------------------

def test_1_log_claims_four_rows_but_only_one_remains_is_not_complete(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    original_rows = [_row(str(i), f"2024.01.0{i} 13:30:00") for i in range(1, 5)]  # 4 rows
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)

    # Damage: truncate the CSV down to just the first row.
    _write_csv(csv_path, original_rows[:1])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert "2024-01" in report.missing_months
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "failed"
    assert "1 canonical" in entry.error and "expected 4" in entry.error
    # Document what the old (row-presence-only) check would have done wrong.
    assert _old_naive_would_certify(log_says_ok=True, current_row_count=1) is True


# -- 2: header-only CSV --------------------------------------------------

def test_2_header_only_csv_is_not_complete(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    original_rows = [_row("1", "2024.01.05 13:30:00")]
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)

    csv_path.write_text(CSV_HEADER + "\n", encoding="utf-8")  # truncate to header only

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert "2024-01" in report.missing_months
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "failed"
    assert "zero rows" in entry.error


# -- 3: CSV deleted while sidecar survives -------------------------------

def test_3_csv_deleted_while_sidecar_log_survives_is_not_complete(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    original_rows = [_row("1", "2024.01.05 13:30:00")]
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)
    assert csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv").exists()

    csv_path.unlink()  # CSV gone; sidecar log untouched

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert "2024-01" in report.missing_months
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "failed"


# -- 4: one value changes without changing row count ---------------------

def test_4_value_changed_without_row_count_change_is_integrity_failure(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    original_rows = [_row("1", "2024.01.05 13:30:00", actual="216.000000")]
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)

    modified_rows = [_row("1", "2024.01.05 13:30:00", actual="999.000000")]  # same id, different value
    _write_csv(csv_path, modified_rows)

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert "2024-01" in report.missing_months
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "failed"
    assert "content digest does not match" in entry.error
    # Document what the old (row-presence-only) check would have done wrong:
    # count is still 1 > 0 and the log still says OK, so it would have
    # wrongly certified the MODIFIED data as complete.
    assert _old_naive_would_certify(log_says_ok=True, current_row_count=1) is True


# -- 5: intact matching data and evidence: complete; exporter can skip ---

def test_5_intact_matching_data_is_complete_and_exporter_resume_skips(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    original_rows = [_row("1", "2024.01.05 13:30:00")]
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert "2024-01" in report.covered_months
    assert manifest.is_complete("mql5", "US:USD", "2024-01-01", "2024-01-31")

    # A real exporter resume (simulated) must skip this window rather
    # than re-fetching it.
    exporter = SimulatedMql5Exporter(csv_path)
    fetch_calls = []
    result = exporter.run([(dt.date(2024, 1, 1), dt.date(2024, 1, 31))], lambda ws, we: fetch_calls.append((ws, we)) or [])
    assert result["results"][0]["status"] == "SKIPPED"
    assert fetch_calls == []


# -- 6: appending another window must not invalidate unchanged windows --

def test_6_appending_another_window_leaves_unchanged_verified_window_valid(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    jan_rows = [_row("1", "2024.01.05 13:30:00")]
    _write_csv(csv_path, jan_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), jan_rows)

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert manifest.is_complete("mql5", "US:USD", "2024-01-01", "2024-01-31")

    # Append February's window -- January's rows/evidence untouched.
    feb_rows = [_row("2", "2024.02.05 13:30:00")]
    all_rows = jan_rows + feb_rows
    _write_csv(csv_path, all_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 2, 1), dt.date(2024, 2, 29), feb_rows)

    manifest2 = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest2, dt.date(2024, 1, 1), dt.date(2024, 2, 29))
    assert "2024-01" in report.covered_months
    assert "2024-02" in report.covered_months
    assert manifest2.is_complete("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert manifest2.is_complete("mql5", "US:USD", "2024-02-01", "2024-02-29")


# -- 7: overlapping exports and revisions follow the documented contract --

def test_7_overlapping_export_with_unchanged_rows_keeps_narrower_window_valid(tmp_path):
    """Sept 1-10 is exported (rows A, B). A later, WIDER, overlapping
    Sept 1-11 export re-observes A, B (unchanged) plus a new C.
    Canonical dedup-by-value_id means the narrower Sept 1-10 window's
    own recorded evidence (over A, B only) is still exactly reproducible
    from the current file, since A/B's content is unchanged."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    row_a = _row("A", "2024.09.02 13:30:00")
    row_b = _row("B", "2024.09.09 13:30:00")
    row_c = _row("C", "2024.09.11 13:30:00")

    _write_csv(csv_path, [row_a, row_b])
    _write_ok_log_entry(csv_path, dt.date(2024, 9, 1), dt.date(2024, 9, 10), [row_a, row_b])

    # Wider overlapping export: A, B re-observed (unchanged) plus new C.
    _write_csv(csv_path, [row_a, row_b, row_c])
    _write_ok_log_entry(csv_path, dt.date(2024, 9, 1), dt.date(2024, 9, 11), [row_a, row_b, row_c])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    # Query the exact wider (Sept 1-11) window the second export logged
    # -- iter_date_chunks would otherwise group a full-month request
    # into one Sept 1-30 chunk that matches NEITHER partial log entry
    # (see test_partial_month_window_does_not_satisfy_a_wider_later_request
    # in test_fetch_mql5.py for that separate, already-covered behavior).
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 9, 1), dt.date(2024, 9, 11))
    assert "2024-09" in report.covered_months


def test_7_revision_updates_canonical_value_via_last_occurrence(tmp_path):
    """The same value_id appearing twice (an original print, then a
    revised reprint) must be treated as ONE canonical record -- the
    LAST occurrence -- not two, and not the first."""
    original = _row("A", "2024.01.05 13:30:00", actual="216.000000")
    revised = _row("A", "2024.01.05 13:30:00", actual="218.000000")
    count, digest = window_content_digest([original, revised])
    assert count == 1  # one canonical record, not two
    # Digest matches hashing ONLY the revised (last) occurrence.
    only_revised_count, only_revised_digest = window_content_digest([revised])
    assert (count, digest) == (only_revised_count, only_revised_digest)


# -- 8: malformed or old-schema logs -> explicit unverified coverage -----

def test_8_old_schema_version_log_is_treated_as_no_usable_log(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    rows = [_row("1", "2024.01.05 13:30:00")]
    _write_csv(csv_path, rows)
    # A v3-shaped (old schema, 10 fields but wrong version number) entry.
    old_log = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    old_log.write_text(
        "2024.01.01 00:00:00,2024.02.01 00:00:00,US,USD,3,OK,1,1,2024.01.01 00:00:00,deadbeef\n",
        encoding="utf-8",
    )

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert report.coverage_inferred is True  # no USABLE (current-schema) log -> explicit fallback
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "provisional"  # never finalized from unverifiable coverage


def test_8_malformed_log_row_is_treated_as_no_matching_entry(tmp_path):
    """A log with at least one well-formed entry (so it isn't treated as
    "no log at all") but whose entry for THIS specific window is
    malformed (missing the digest field) must still not certify THIS
    window -- malformed evidence for one window must not be papered
    over just because other entries in the same log are fine."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    jan_rows = [_row("1", "2024.01.05 13:30:00")]
    feb_rows = [_row("2", "2024.02.05 13:30:00")]
    _write_csv(csv_path, jan_rows + feb_rows)

    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    # January: missing the digest field entirely (9 fields instead of 10).
    malformed_jan_line = f"2024.01.01 00:00:00,2024.02.01 00:00:00,US,USD,{MQL5_EXPORT_SCHEMA_VERSION},OK,1,1,2024.01.01 00:00:00"
    log_path.write_text(malformed_jan_line + "\n", encoding="utf-8")
    # February: a genuinely well-formed entry, so the log as a whole is
    # NOT empty/unusable (coverage_inferred stays False).
    _write_ok_log_entry(csv_path, dt.date(2024, 2, 1), dt.date(2024, 2, 29), feb_rows)

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 2, 29))

    assert report.coverage_inferred is False
    assert "2024-02" in report.covered_months  # the well-formed entry still works
    assert "2024-01" in report.missing_months  # the malformed one does not certify anything
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "failed"


# -- 9: simulated unsuccessful data write -> no successful checkpoint ----

def test_9_simulated_write_failure_produces_no_successful_checkpoint(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    window = (dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    result = exporter.run(
        [window], lambda ws, we: [_row("1", "2024.01.05 13:30:00")],
        simulate_write_failure_for={window},
    )
    assert result["results"][0]["status"] == "WRITE_FAILED"
    assert not exporter.log_path.exists()  # no checkpoint evidence written at all

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert "2024-01" in report.missing_months


# -- 10: repairing damaged data restores verified completion -------------

def test_10_repairing_damaged_data_restores_verified_completion(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    original_rows = [_row("1", "2024.01.05 13:30:00")]
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)

    # Damage it (partial truncation to header only).
    csv_path.write_text(CSV_HEADER + "\n", encoding="utf-8")

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    damaged_report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert "2024-01" in damaged_report.missing_months

    # Repair: a real re-export restores the data AND writes fresh,
    # correct evidence (never trusts/repairs the stale log entry itself).
    _write_csv(csv_path, original_rows)
    _write_ok_log_entry(csv_path, dt.date(2024, 1, 1), dt.date(2024, 1, 31), original_rows)

    manifest2 = Manifest(config.manifest_path)
    repaired_report = ingest_mql5_calendar(config, manifest2, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert "2024-01" in repaired_report.covered_months
    assert manifest2.is_complete("mql5", "US:USD", "2024-01-01", "2024-01-31")
