"""Regression tests: raw-byte preservation and charset-aware decoding
for manually-saved Forex Factory HTML.

BACKGROUND: a real saved page can declare `<meta charset="windows-1252">`
(this is the encoding many browsers historically defaulted to for pages
that don't explicitly declare UTF-8). The importer previously did:

    html = raw_bytes.decode("utf-8", errors="replace")
    ...
    attempt_path.write_text(html, encoding="utf-8")

Two separate defects: (1) `errors="replace"` silently turns any
Windows-1252 byte with no UTF-8 meaning (curly quotes, en/em dashes,
accented letters -- exactly the provenance-sensitive text this pipeline
cares about) into U+FFFD, destroying information the raw bytes never
actually lost; (2) re-encoding the decoded text back to UTF-8 before
writing the "raw" archival copy means that archived file is no longer
byte-identical to what the user actually downloaded -- provenance
fiction, similar in spirit to every other "never re-derive, always
preserve the original artifact" rule already enforced elsewhere in this
pipeline (see fetch/forex_factory.py's own RAW IMMUTABILITY note).

Fixed: `decode_html_bytes` (normalize/forex_factory.py) tries the page's
own declared charset, then UTF-8, then CP1252, all strict, falling back
to Latin-1 (which never raises and never discards a byte) only if every
named encoding fails -- and the importer archives `raw_bytes` verbatim,
checksumming those same original bytes, never a re-encoding.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from src.data.config import AppConfig
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest, checksum_bytes
from src.data.normalize.forex_factory import decode_html_bytes, detect_declared_charset

import import_forex_factory as importer  # noqa: E402

# A string exercising all three required cases at once: an accented
# character (é, U+00E9), a curly quote (’, U+2019), and an en dash
# (–, U+2013).
SPECIAL_TEXT = "Fed Chair’s Testimony – Café Edition"


def _html_bytes(body_text: str, charset_label: str, encoding: str, legacy_http_equiv: bool = False) -> bytes:
    if legacy_http_equiv:
        meta = f'<meta http-equiv="Content-Type" content="text/html; charset={charset_label}">'
    else:
        meta = f'<meta charset="{charset_label}">'
    # Padded well past _cheap_sanity_check's 500-byte floor.
    padding = "<!-- " + ("padding " * 80) + " -->"
    html = f"<html><head><title>Calendar | Forex Factory</title>{meta}</head><body>{padding}<p>{body_text}</p></body></html>"
    return html.encode(encoding)


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


# =====================================================================
# Unit tests: detect_declared_charset / decode_html_bytes directly.
# =====================================================================

def test_utf8_saved_page_decodes_correctly():
    raw = _html_bytes(SPECIAL_TEXT, "utf-8", "utf-8")
    assert detect_declared_charset(raw) in ("utf-8", "UTF-8")
    decoded = decode_html_bytes(raw)
    assert SPECIAL_TEXT in decoded
    assert "�" not in decoded


def test_windows_1252_saved_page_decodes_correctly():
    raw = _html_bytes(SPECIAL_TEXT, "windows-1252", "cp1252")
    assert detect_declared_charset(raw).lower() in ("windows-1252",)
    decoded = decode_html_bytes(raw)
    assert SPECIAL_TEXT in decoded
    assert "�" not in decoded


def test_legacy_http_equiv_charset_detected():
    raw = _html_bytes(SPECIAL_TEXT, "windows-1252", "cp1252", legacy_http_equiv=True)
    assert detect_declared_charset(raw).lower() == "windows-1252"
    decoded = decode_html_bytes(raw)
    assert SPECIAL_TEXT in decoded


def test_accented_character_preserved():
    raw = _html_bytes("Café", "windows-1252", "cp1252")
    decoded = decode_html_bytes(raw)
    assert "Café" in decoded


def test_curly_quote_and_en_dash_preserved():
    raw = _html_bytes("Bull’s Run – 2024", "windows-1252", "cp1252")
    decoded = decode_html_bytes(raw)
    assert "Bull’s Run – 2024" in decoded


def test_no_replacement_character_for_valid_cp1252_input_without_declaration():
    """Even with NO <meta charset> at all, valid CP1252 bytes that are
    NOT valid UTF-8 must still decode via the CP1252 fallback, never
    produce U+FFFD."""
    html_no_meta = (
        "<html><head><title>Calendar | Forex Factory</title></head><body>"
        + "<!-- " + ("padding " * 80) + " -->"
        + f"<p>{SPECIAL_TEXT}</p></body></html>"
    )
    raw = html_no_meta.encode("cp1252")
    assert detect_declared_charset(raw) is None
    decoded = decode_html_bytes(raw)
    assert SPECIAL_TEXT in decoded
    assert "�" not in decoded


def test_missing_declaration_valid_utf8_still_decodes_as_utf8():
    html_no_meta = (
        "<html><head><title>Calendar | Forex Factory</title></head><body>"
        + "<!-- " + ("padding " * 80) + " -->"
        + f"<p>{SPECIAL_TEXT}</p></body></html>"
    )
    raw = html_no_meta.encode("utf-8")
    assert detect_declared_charset(raw) is None
    decoded = decode_html_bytes(raw)
    assert SPECIAL_TEXT in decoded
    assert "�" not in decoded


def test_declared_charset_wrong_falls_through_safely():
    """A page LYING about its own charset (declares utf-8 but is
    actually cp1252 bytes) must not crash -- the declared-charset strict
    attempt fails, and the fallback chain still recovers the text
    without a replacement character."""
    raw = _html_bytes(SPECIAL_TEXT, "utf-8", "cp1252")  # mismatched on purpose
    decoded = decode_html_bytes(raw)
    assert "�" not in decoded
    # Recovered via the cp1252 fallback step, not the (wrong) declared one.
    assert SPECIAL_TEXT in decoded


# =====================================================================
# End-to-end: importer preserves raw bytes exactly and checksums them,
# never a re-encoded representation.
# =====================================================================

def _event(event_id, ebase_id, name, dateline, time_label, date_str, currency="USD"):
    return {
        "id": event_id, "ebaseId": ebase_id, "name": name, "prefixedName": f"US {name}",
        "notice": "", "dateline": dateline, "country": "US", "currency": currency,
        "impactClass": "icon--ff-impact-red", "impactTitle": "High Impact Expected",
        "timeLabel": time_label, "timeMasked": False,
        "actual": "1.0%", "forecast": "1.1%", "previous": "0.9%", "revision": "",
        "date": date_str,
    }


def _day(date_label, dateline, events=None):
    return {"date": date_label, "dateline": dateline, "add": "", "events": events or []}


def _dateline(y, m, d, hour=0, minute=0):
    from zoneinfo import ZoneInfo
    local = dt.datetime(y, m, d, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    return int(local.timestamp())


def _cp1252_page_bytes() -> bytes:
    import json

    days = [_day(f"Day <span>Jan {d}</span>", _dateline(2016, 1, d)) for d in range(1, 32)]
    days[7]["events"] = [_event(108, 66, "Non-Farm Employment Change", _dateline(2016, 1, 8, 8, 30), "8:30am", "Jan 8, 2016")]
    padding = "<!-- " + ("padding " * 80) + " -->"
    ff_settings = (
        "<script>window.FF = {country_code: 'TR', timezone: '-4'.replace('+', ''), "
        "timezone_name: 'America/New_York', display_time: '7:45am'}</script>"
    )
    # A CP1252-only sentence sitting in the visible page body, separate
    # from the (ASCII-only, structurally JSON) calendar state -- this is
    # what actually needs charset-aware decoding on a real saved page.
    body_note = f"<p>{SPECIAL_TEXT}</p>"
    html = (
        f"<html><head><title>Calendar | Forex Factory</title>"
        f'<meta charset="windows-1252">{ff_settings}</head><body>{padding}{body_note}'
        "<script>if (typeof window.calendarComponentStates === 'undefined') { window.calendarComponentStates = {} }\n"
        "window.calendarComponentStates[1] = {\n"
        f"days: {json.dumps(days)},\n"
        "time: '7:45am', upNext: 'sep13.2026'\n"
        "}</script></body></html>"
    )
    return html.encode("cp1252")


@pytest.fixture
def event_mapping():
    return load_event_mapping()


def test_raw_checksum_equals_checksum_of_source_bytes(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    raw = _cp1252_page_bytes()
    html_path = tmp_path / "cp1252.html"
    html_path.write_bytes(raw)

    log = importer._load_import_log(config)
    result = importer.import_file(config, manifest, event_mapping, log, html_path, dt.date(2026, 1, 1))

    assert result.status == "imported"
    assert result.checksum == checksum_bytes(raw)
    entries = manifest.entries_for("forex_factory")
    assert entries
    for entry in entries:
        assert entry.checksum == checksum_bytes(raw)


def test_archived_raw_file_bytes_exactly_equal_source_bytes(tmp_path, event_mapping):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    raw = _cp1252_page_bytes()
    html_path = tmp_path / "cp1252.html"
    html_path.write_bytes(raw)

    log = importer._load_import_log(config)
    result = importer.import_file(config, manifest, event_mapping, log, html_path, dt.date(2026, 1, 1))
    assert result.status == "imported"

    entries = manifest.entries_for("forex_factory")
    assert entries
    for entry in entries:
        archived_bytes = Path(entry.path).read_bytes()
        assert archived_bytes == raw  # byte-for-byte, not a UTF-8 re-encoding


def test_source_html_file_itself_never_modified(tmp_path, event_mapping):
    """The user's original downloaded file must never be written to."""
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    raw = _cp1252_page_bytes()
    html_path = tmp_path / "cp1252.html"
    html_path.write_bytes(raw)
    mtime_before = html_path.stat().st_mtime

    log = importer._load_import_log(config)
    importer.import_file(config, manifest, event_mapping, log, html_path, dt.date(2026, 1, 1))

    assert html_path.read_bytes() == raw
    assert html_path.stat().st_mtime == mtime_before


def test_cp1252_page_imports_successfully_with_correct_events(tmp_path, event_mapping):
    """End-to-end: a CP1252-declared real-shaped page still imports
    correctly (structured data lives in the ASCII-only JSON portion, so
    charset only affects the surrounding page text) -- confirms the
    encoding fix doesn't regress ordinary structured-data parsing."""
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    raw = _cp1252_page_bytes()
    html_path = tmp_path / "cp1252.html"
    html_path.write_bytes(raw)

    log = importer._load_import_log(config)
    result = importer.import_file(config, manifest, event_mapping, log, html_path, dt.date(2026, 1, 1))

    assert result.status == "imported"
    assert manifest.is_complete("forex_factory", "ALL", "2016-01-01", "2016-01-31")
    assert result.stats["usd_event_count"] == 1
    assert result.stats["mapped_usd_event_count"] == 1
