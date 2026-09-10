import datetime as dt

from src.data.manifest import Manifest, ManifestEntry


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


def test_latest_complete_end_ignores_failed_and_other_keys(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="complete"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-02-01", end="2020-02-29", status="complete"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-03-01", end="2020-03-31", status="failed"))
    m.record(ManifestEntry(provider="massive", key="SPY", start="2020-06-01", end="2020-06-30", status="complete"))

    assert m.latest_complete_end("massive", "QQQ") == dt.date(2020, 2, 29)
    assert m.latest_complete_end("massive", "SPY") == dt.date(2020, 6, 30)
    assert m.latest_complete_end("massive", "IWM") is None


def test_re_recording_same_chunk_overwrites_not_duplicates(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="failed", error="boom"))
    m.record(ManifestEntry(provider="massive", key="QQQ", start="2020-01-01", end="2020-01-31", status="complete", rows=500))

    assert len(m.entries_for("massive", "QQQ")) == 1
    assert m.get("massive", "QQQ", "2020-01-01", "2020-01-31").status == "complete"
