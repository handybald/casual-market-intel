import datetime as dt

from src.data.manifest import Manifest, ManifestEntry, checksum_bytes


def test_record_and_is_complete(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    assert not m.is_complete("massive", "QQQ", "2020-01-01", "2020-01-31")

    m.record(
        ManifestEntry(
            provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31",
            status="complete", rows=100,
        )
    )
    assert m.is_complete("massive", "QQQ", "2020-01-01", "2020-01-31")
    assert not m.is_complete("massive", "SPY", "2020-01-01", "2020-01-31")


def test_manifest_persists_and_reloads(tmp_path):
    path = tmp_path / "manifest.json"
    m1 = Manifest(path)
    m1.record(
        ManifestEntry(provider="fred", key="CPIAUCSL", start="2016-01-01", end="2026-09-10", status="complete", rows=120)
    )

    m2 = Manifest(path)  # simulate a fresh process resuming
    assert m2.is_complete("fred", "CPIAUCSL", "2016-01-01", "2026-09-10")
    entry = m2.get("fred", "CPIAUCSL", "2016-01-01", "2026-09-10")
    assert entry.rows == 120


def test_empty_status_counts_as_complete_for_resume(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-07-01", end="2020-07-31", status="empty"))
    assert m.is_complete("massive", "QQQ", "2020-07-01", "2020-07-31")


def test_failed_status_does_not_count_as_complete(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-07-01", end="2020-07-31", status="failed", error="timeout"))
    assert not m.is_complete("massive", "QQQ", "2020-07-01", "2020-07-31")
    assert len(m.failed_entries("massive")) == 1


def test_provisional_status_is_never_complete_and_always_retried(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2024-09-01", end="2024-09-10", status="provisional", rows=50))
    assert not m.is_complete("massive", "QQQ", "2024-09-01", "2024-09-10")
    assert len(m.provisional_entries("massive")) == 1


def test_re_recording_same_chunk_overwrites_not_duplicates(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="failed", error="boom"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="complete", rows=500))

    assert len(m.entries_for("massive", "QQQ")) == 1
    assert m.get("massive", "QQQ", "2020-01-01", "2020-01-31").status == "complete"


# -- artifact integrity (regression: deleted/corrupt artifact, stale shared checksum) --

def test_deleted_artifact_is_not_treated_as_complete(tmp_path):
    artifact = tmp_path / "QQQ_2020.parquet"
    artifact.write_bytes(b"fake parquet bytes")
    m = Manifest(tmp_path / "manifest.json")
    m.record(
        ManifestEntry(
            provider="massive", key="QQQ:1min:raw", start="2020-01-01", end="2020-01-31",
            status="complete", rows=100, path=str(artifact), checksum=checksum_bytes(b"fake parquet bytes"),
        )
    )
    assert m.is_complete("massive", "QQQ:1min:raw", "2020-01-01", "2020-01-31")

    artifact.unlink()
    assert not m.is_complete("massive", "QQQ:1min:raw", "2020-01-01", "2020-01-31")


def test_corrupted_artifact_checksum_mismatch_is_not_complete(tmp_path):
    artifact = tmp_path / "QQQ_2020.parquet"
    artifact.write_bytes(b"original bytes")
    m = Manifest(tmp_path / "manifest.json")
    m.record(
        ManifestEntry(
            provider="massive", key="QQQ:1min:raw", start="2020-01-01", end="2020-01-31",
            status="complete", rows=100, path=str(artifact), checksum=checksum_bytes(b"original bytes"),
        )
    )
    artifact.write_bytes(b"corrupted/truncated bytes")
    assert not m.is_complete("massive", "QQQ:1min:raw", "2020-01-01", "2020-01-31")


def test_update_checksum_for_path_refreshes_all_sharing_entries(tmp_path):
    artifact = tmp_path / "QQQ_2020.parquet"
    artifact.write_bytes(b"jan+feb data")
    m = Manifest(tmp_path / "manifest.json")
    m.record(
        ManifestEntry(provider="massive", key="QQQ:1min:raw", start="2020-01-01", end="2020-01-31",
                      status="complete", path=str(artifact), checksum=checksum_bytes(b"jan+feb data"))
    )
    m.record(
        ManifestEntry(provider="massive", key="QQQ:1min:raw", start="2020-02-01", end="2020-02-29",
                      status="complete", path=str(artifact), checksum=checksum_bytes(b"jan+feb data"))
    )

    # File is rewritten to add March -> checksum changes for the shared file.
    artifact.write_bytes(b"jan+feb+mar data")
    m.update_checksum_for_path(artifact, checksum_bytes(b"jan+feb+mar data"))

    assert m.is_complete("massive", "QQQ:1min:raw", "2020-01-01", "2020-01-31")
    assert m.is_complete("massive", "QQQ:1min:raw", "2020-02-01", "2020-02-29")


def test_without_checksum_refresh_stale_entries_fail_verification(tmp_path):
    artifact = tmp_path / "QQQ_2020.parquet"
    artifact.write_bytes(b"jan data")
    m = Manifest(tmp_path / "manifest.json")
    m.record(
        ManifestEntry(provider="massive", key="QQQ:1min:raw", start="2020-01-01", end="2020-01-31",
                      status="complete", path=str(artifact), checksum=checksum_bytes(b"jan data"))
    )
    artifact.write_bytes(b"jan+feb data")  # rewritten, but checksum NOT refreshed
    assert not m.is_complete("massive", "QQQ:1min:raw", "2020-01-01", "2020-01-31")


# -- interval-aware watermark / gap repair (regression: failed July, successful August) --

def test_watermark_is_none_when_dataset_start_uncovered(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    assert m.completion_watermark("massive", "QQQ", dt.date(2020, 1, 1)) is None


def test_watermark_advances_through_contiguous_coverage(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="complete"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-02-01", end="2020-02-29", status="complete"))
    assert m.completion_watermark("massive", "QQQ", dt.date(2020, 1, 1)) == dt.date(2020, 2, 29)


def test_failed_month_holds_watermark_even_if_later_month_succeeds(tmp_path):
    """Reproduces: July fails, August succeeds -> watermark must NOT jump to
    August, or the next update would start in September and July is lost
    forever."""
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-06-01", end="2020-06-30", status="complete"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-07-01", end="2020-07-31", status="failed", error="timeout"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-08-01", end="2020-08-31", status="complete"))

    watermark = m.completion_watermark("massive", "QQQ", dt.date(2020, 6, 1))
    assert watermark == dt.date(2020, 6, 30)  # NOT August 31


def test_repairing_failed_interval_advances_watermark(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-06-01", end="2020-06-30", status="complete"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-07-01", end="2020-07-31", status="failed"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-08-01", end="2020-08-31", status="complete"))
    assert m.completion_watermark("massive", "QQQ", dt.date(2020, 6, 1)) == dt.date(2020, 6, 30)

    # July repaired.
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-07-01", end="2020-07-31", status="complete"))
    assert m.completion_watermark("massive", "QQQ", dt.date(2020, 6, 1)) == dt.date(2020, 8, 31)


def test_coverage_gaps_identifies_failed_and_missing_ranges(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-06-01", end="2020-06-30", status="complete"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-07-01", end="2020-07-31", status="failed"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-08-01", end="2020-08-31", status="complete"))

    gaps = m.coverage_gaps("massive", "QQQ", dt.date(2020, 6, 1), dt.date(2020, 9, 30))
    assert gaps == [(dt.date(2020, 7, 1), dt.date(2020, 7, 31)), (dt.date(2020, 9, 1), dt.date(2020, 9, 30))]


def test_coverage_gaps_treats_provisional_as_a_gap(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-09-01", end="2020-09-10", status="provisional"))
    gaps = m.coverage_gaps("massive", "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10))
    assert gaps == [(dt.date(2020, 9, 1), dt.date(2020, 9, 10))]


def test_coverage_gaps_empty_when_fully_covered(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-12-31", status="complete"))
    gaps = m.coverage_gaps("massive", "QQQ", dt.date(2020, 1, 1), dt.date(2020, 12, 31))
    assert gaps == []


# -- classify_gaps: distinguish successful-provisional from real failures (second review item #2) --

def test_classify_gaps_provisional_is_not_a_failure(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-09-01", end="2020-09-10", status="provisional"))
    gaps = m.classify_gaps("massive", "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10), today=dt.date(2020, 9, 10))
    assert len(gaps) == 1
    assert gaps[0].reason == "provisional"
    assert gaps[0].is_failure is False


def test_classify_gaps_failed_entry_is_a_failure(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-09-01", end="2020-09-10", status="failed"))
    gaps = m.classify_gaps("massive", "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10), today=dt.date(2020, 9, 10))
    assert gaps[0].reason == "failed"
    assert gaps[0].is_failure is True


def test_classify_gaps_never_attempted_is_missing_and_a_failure(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    gaps = m.classify_gaps("massive", "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), today=dt.date(2021, 1, 1))
    assert gaps[0].reason == "missing"
    assert gaps[0].is_failure is True


def test_classify_gaps_future_range_is_not_a_failure(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    gaps = m.classify_gaps("massive", "QQQ", dt.date(2030, 1, 1), dt.date(2030, 1, 31), today=dt.date(2020, 1, 1))
    assert gaps[0].reason == "future"
    assert gaps[0].is_failure is False


def test_classify_gaps_repaired_interval_produces_no_gap(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="complete"))
    gaps = m.classify_gaps("massive", "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), today=dt.date(2021, 1, 1))
    assert gaps == []
