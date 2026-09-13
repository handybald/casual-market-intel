"""Regression tests (sixth review, P2): the MQL5 window-integrity digest
contract hashed DIFFERENT byte sequences on the two sides for any
non-ASCII text -- Python's `_fnv1a_64` hashes UTF-8-encoded bytes
(`_canonical_row_string(...).encode("utf-8")`), while the .mq5 file's
`Fnv1a64` hashed the raw values returned by `StringGetCharacter()`,
which are UTF-16 CODE UNITS, not UTF-8 bytes. For any character outside
plain ASCII (e.g. "e" with an acute accent, "é") these two are
numerically different, so an intact, unmodified export containing such
text would fail integrity verification purely due to encoding
mismatch -- indistinguishable from real corruption.

Compounding this: the exporter used FILE_ANSI mode (the OS's system
codepage, e.g. Windows-1252) for all CSV/log reads and writes, while
Python opens the same files with `encoding="utf-8"`. A byte sequence
that is valid Windows-1252 is frequently NOT valid UTF-8 at all (a
lone 0xE9 byte, "e" with an acute accent in Windows-1252, is an
incomplete/invalid UTF-8 sequence on its own), so an intact export
could fail to even DECODE on the Python side, not just fail the digest
check.

THE FIX (see src/data/fetch/mql5.py's module docstring and
mql5_exporter/EconomicCalendarExporter.mq5's header comment for the
full contract):
  - CSV/log text is UTF-8 on both sides, always.
  - The .mq5 file now performs all CSV/log file I/O in FILE_BIN mode,
    converting explicitly via StringToCharArray(..., CP_UTF8_CODEPAGE)
    / CharArrayToString(..., CP_UTF8_CODEPAGE) -- MQL5's FILE_ANSI/
    FILE_UNICODE text modes cannot produce UTF-8 bytes at all (ANSI is
    a single-byte system codepage; UNICODE is UTF-16LE).
  - StringToCharArray appends a trailing NUL terminator by default;
    every conversion site explicitly excludes it (both from what is
    written to disk and from what is hashed).
  - The digest itself is unchanged algorithmically (FNV-1a 64,
    order-independent XOR-combine, canonical-record dedup by
    value_id/last-occurrence-wins) -- only the BYTE SOURCE changes,
    from UTF-16 code units to real UTF-8 bytes.
  - EXPORT_SCHEMA_VERSION bumped 4 -> 5 (a different window-log
    filename) specifically because old v4 evidence could have been
    computed from ANSI-encoded bytes for any non-ASCII content and
    must never be silently re-certified under the new byte-accurate
    algorithm -- see the module docstring for the full compatibility
    argument (pure-ASCII v4 evidence happens to be byte-identical
    either way, but the version bump treats ALL old evidence as
    unverified, deliberately not relying on that coincidence).

TEST VECTOR PROVENANCE: every expected canonical string / UTF-8 byte
sequence / digest value below was computed by an INDEPENDENT, from-
scratch reference script (not part of this repository, not calling
`_fnv1a_64`/`_canonical_row_string`/`window_content_digest`) and pasted
in as a literal constant -- these tests would catch a bug in the
production implementation itself, not just confirm it agrees with
itself. The reference algorithm: FNV-1a 64-bit (offset basis
0xcbf29ce484222325, prime 0x100000001b3, standard byte-at-a-time
update) over the UTF-8 encoding of `"\x1f".join(row[col] for col in
CSV_COLUMNS)`.
"""
from pathlib import Path

from src.data.fetch.mql5 import (
    CSV_COLUMNS,
    _canonical_row_string,
    _fnv1a_64,
    window_content_digest,
)

MQL5_SELF_TEST_SOURCE = (
    Path(__file__).resolve().parents[1] / "mql5_exporter" / "DigestSelfTest.mq5"
).read_text(encoding="utf-8")


def _old_buggy_fnv1a_64_utf16_code_units(s: str) -> int:
    """Reproduces the ORIGINAL (pre-sixth-review) .mq5 `Fnv1a64(string)`:
    it iterated `StringGetCharacter(s, i)` for `i` in `[0, StringLen(s))`.
    MQL5 strings are UTF-16 internally, so this yields one UTF-16 CODE
    UNIT per iteration (a surrogate pair = two iterations for a
    supplementary-plane character) -- NOT one UTF-8 byte, which is what
    Python's `_fnv1a_64` (and the FIXED .mq5 file, via
    StringToCharArray(..., CP_UTF8)) actually hashes. Used ONLY as a
    documented point of comparison to concretely demonstrate the
    confirmed defect -- not executed production code."""
    utf16_bytes = s.encode("utf-16-le")
    code_units = [
        utf16_bytes[i] | (utf16_bytes[i + 1] << 8)
        for i in range(0, len(utf16_bytes), 2)
    ]
    _MASK_64 = 0xFFFFFFFFFFFFFFFF
    h = 0xCBF29CE484222325
    for cu in code_units:
        h = h ^ cu
        h = (h * 0x100000001B3) & _MASK_64
    return h


def _row(**overrides):
    row = {
        "value_id": "1", "event_id": "100", "event_name": "NFP", "country_code": "US",
        "currency_code": "USD", "importance": "HIGH", "event_time": "2024.01.05 13:30:00",
        "period": "2023.12.01", "unit": "JOB", "multiplier": "THOUSANDS",
        "actual_value": "216.000000", "forecast_value": "170.000000", "prev_value": "173.000000",
        "revised_prev_value": "", "revision": "0", "source_timezone": "SERVER",
    }
    row.update(overrides)
    return row


def test_csv_columns_order_matches_the_vectors_below():
    """Guards every vector below: if CSV_COLUMNS order ever changes,
    these hard-coded expected strings/digests must be recomputed --
    fail loudly here rather than have every vector test silently drift."""
    assert CSV_COLUMNS == [
        "value_id", "event_id", "event_name", "country_code", "currency_code", "importance",
        "event_time", "period", "unit", "multiplier", "actual_value", "forecast_value",
        "prev_value", "revised_prev_value", "revision", "source_timezone",
    ]


# -- 1: ASCII text --

def test_vector_1_ascii():
    row = _row()
    expected_string = (
        "1\x1f100\x1fNFP\x1fUS\x1fUSD\x1fHIGH\x1f2024.01.05 13:30:00\x1f2023.12.01\x1fJOB\x1f"
        "THOUSANDS\x1f216.000000\x1f170.000000\x1f173.000000\x1f\x1f0\x1fSERVER"
    )
    expected_bytes_hex = (
        "311f3130301f4e46501f55531f5553441f484947481f323032342e30312e30352031333a33303a3030"
        "1f323032332e31322e30311f4a4f421f54484f5553414e44531f3231362e3030303030301f3137302e"
        "3030303030301f3137332e3030303030301f1f301f534552564552"
    )
    expected_digest = "dd0c0fd6dcf04073"

    canonical = _canonical_row_string(row)
    assert canonical == expected_string
    assert canonical.encode("utf-8").hex() == expected_bytes_hex
    assert f"{_fnv1a_64(canonical.encode('utf-8')):016x}" == expected_digest
    assert window_content_digest([row]) == (1, expected_digest)


# -- 2: accented text (e-acute) --

def test_vector_2_accented():
    row = _row(event_name="Café Price Index")
    expected_bytes_hex = (
        "311f3130301f436166c3a920507269636520496e6465781f55531f5553441f484947481f323032342e"
        "30312e30352031333a33303a30301f323032332e31322e30311f4a4f421f54484f5553414e44531f32"
        "31362e3030303030301f3137302e3030303030301f3137332e3030303030301f1f301f534552564552"
    )
    expected_digest = "1e22ead2ffe06d90"

    canonical = _canonical_row_string(row)
    assert canonical.encode("utf-8").hex() == expected_bytes_hex
    assert window_content_digest([row]) == (1, expected_digest)


# -- 3: curly apostrophe and em dash --

def test_vector_3_curly_apostrophe_and_em_dash():
    row = _row(event_name="Fed Chair’s Speech — Q&A")
    expected_bytes_hex = (
        "311f3130301f466564204368616972e28099732053706565636820e28094205126411f55531f5553441f"
        "484947481f323032342e30312e30352031333a33303a30301f323032332e31322e30311f4a4f421f5448"
        "4f5553414e44531f3231362e3030303030301f3137302e3030303030301f3137332e3030303030301f1f"
        "301f534552564552"
    )
    expected_digest = "c23b545ef1a66d93"

    canonical = _canonical_row_string(row)
    assert canonical.encode("utf-8").hex() == expected_bytes_hex
    assert window_content_digest([row]) == (1, expected_digest)


# -- 4: non-Latin character (Japanese) --

def test_vector_4_non_latin():
    row = _row(event_name="日本銀行")  # 日本銀行
    expected_bytes_hex = (
        "311f3130301fe697a5e69cace98a80e8a18c1f55531f5553441f484947481f323032342e30312e3035"
        "2031333a33303a30301f323032332e31322e30311f4a4f421f54484f5553414e44531f3231362e3030"
        "303030301f3137302e3030303030301f3137332e3030303030301f1f301f534552564552"
    )
    expected_digest = "691d4a3aa43c37e7"

    canonical = _canonical_row_string(row)
    assert canonical.encode("utf-8").hex() == expected_bytes_hex
    assert window_content_digest([row]) == (1, expected_digest)


# -- 5: supplementary-plane Unicode character (emoji, beyond the BMP) --

def test_vector_5_supplementary_plane_character():
    """This is the vector that most directly exposes the original bug:
    a character outside the Basic Multilingual Plane is represented in
    UTF-16 (what StringGetCharacter returns, one code unit at a time)
    as a SURROGATE PAIR (two ushort values), but as a single 4-byte
    sequence in UTF-8 -- hashing UTF-16 code units one-at-a-time can
    never reproduce hashing the real UTF-8 byte sequence for this case,
    no matter how ASCII-adjacent the rest of the text is."""
    row = _row(event_name="Rate Decision \U0001F600")  # 😀 U+1F600
    expected_bytes_hex = (
        "311f3130301f52617465204465636973696f6e20f09f98801f55531f5553441f484947481f323032342e"
        "30312e30352031333a33303a30301f323032332e31322e30311f4a4f421f54484f5553414e44531f3231"
        "362e3030303030301f3137302e3030303030301f3137332e3030303030301f1f301f534552564552"
    )
    expected_digest = "4d93af3ffe99600c"

    canonical = _canonical_row_string(row)
    assert canonical.encode("utf-8").hex() == expected_bytes_hex
    assert window_content_digest([row]) == (1, expected_digest)


# -- 6: commas and embedded quotes --

def test_vector_6_commas_and_embedded_quotes():
    """The canonical string joins already-PARSED field values (post
    CSV-unescaping) with the ASCII unit separator -- a raw comma/quote
    inside a field's real text content is preserved as-is here (CSV
    escaping is a SEPARATE, file-encoding-level concern handled when
    the row is written/read as a CSV line, not part of the canonical
    join)."""
    row = _row(event_name='Fed Says, "Rates Steady"')
    expected_bytes_hex = (
        "311f3130301f46656420536179732c2022526174657320537465616479221f55531f5553441f48494748"
        "1f323032342e30312e30352031333a33303a30301f323032332e31322e30311f4a4f421f54484f555341"
        "4e44531f3231362e3030303030301f3137302e3030303030301f3137332e3030303030301f1f301f5345"
        "52564552"
    )
    expected_digest = "8871a54e242c993d"

    canonical = _canonical_row_string(row)
    assert canonical.encode("utf-8").hex() == expected_bytes_hex
    assert window_content_digest([row]) == (1, expected_digest)


# -- 7: empty fields --

def test_vector_7_empty_fields():
    row = _row(forecast_value="", prev_value="", revised_prev_value="")
    expected_string = (
        "1\x1f100\x1fNFP\x1fUS\x1fUSD\x1fHIGH\x1f2024.01.05 13:30:00\x1f2023.12.01\x1fJOB\x1f"
        "THOUSANDS\x1f216.000000\x1f\x1f\x1f\x1f0\x1fSERVER"
    )
    expected_digest = "9a6c9654149e1444"

    canonical = _canonical_row_string(row)
    assert canonical == expected_string
    assert window_content_digest([row]) == (1, expected_digest)


# -- 8: duplicate value_id with a later revised record --

def test_vector_8_duplicate_id_later_revision_wins():
    original = _row(value_id="42", actual_value="216.000000")
    revised = _row(value_id="42", actual_value="218.000000")

    combined_count, combined_digest = window_content_digest([original, revised])
    only_revised_count, only_revised_digest = window_content_digest([revised])

    expected_digest = "53ee8bf2392f4c12"
    assert (combined_count, combined_digest) == (1, expected_digest)
    # The last occurrence alone must produce the IDENTICAL digest --
    # proves last-occurrence-wins, not e.g. an order-dependent XOR of
    # both occurrences accidentally landing on the same value.
    assert (only_revised_count, only_revised_digest) == (1, expected_digest)


# -- concrete demonstration of the confirmed defect (old vs. new) -------

def test_old_utf16_code_unit_hash_disagrees_with_correct_utf8_hash_for_non_ascii():
    """Directly reproduces the confirmed mismatch: for pure-ASCII
    content the old (UTF-16-code-unit) and correct (UTF-8-byte)
    algorithms happen to agree (ASCII code points are numerically
    identical in both encodings), which is exactly why the bug was easy
    to miss with ASCII-only fixtures. For ANY non-ASCII text they
    genuinely disagree -- an intact, unmodified export containing such
    text would have failed integrity verification under the old
    algorithm, indistinguishable from real corruption."""
    ascii_row = _row()
    ascii_canonical = _canonical_row_string(ascii_row)
    correct_ascii_digest = _fnv1a_64(ascii_canonical.encode("utf-8"))
    old_ascii_digest = _old_buggy_fnv1a_64_utf16_code_units(ascii_canonical)
    assert old_ascii_digest == correct_ascii_digest  # coincidental agreement for ASCII

    for event_name in (
        "Café Price Index",                      # accented (vector 2)
        "Fed Chair’s Speech — Q&A",          # curly apostrophe + em dash (vector 3)
        "日本銀行",                     # non-Latin (vector 4)
        "Rate Decision \U0001F600",                     # supplementary plane (vector 5)
    ):
        row = _row(event_name=event_name)
        canonical = _canonical_row_string(row)
        correct_digest = _fnv1a_64(canonical.encode("utf-8"))
        old_digest = _old_buggy_fnv1a_64_utf16_code_units(canonical)
        assert old_digest != correct_digest, f"expected a mismatch for {event_name!r}"


# -- mql5_exporter/DigestSelfTest.mq5 consistency (static source check) --
#
# DigestSelfTest.mq5 is a standalone MQL5 script meant to be compiled
# and run INSIDE MetaTrader (see its own header comment for the manual
# procedure) -- this repository has no MetaTrader compiler/runtime, so
# it cannot be executed here. What CAN be verified here is that its
# hard-coded expected digest constants are transcribed correctly
# against the SAME independently-computed ground truth these Python
# vectors use -- catching a transcription error in the .mq5 file
# without needing to compile it.

def test_mql5_self_test_vectors_match_the_same_expected_digests():
    expected_digests = {
        "1_ascii": "dd0c0fd6dcf04073",
        "2_accented": "1e22ead2ffe06d90",
        "3_curly_emdash": "c23b545ef1a66d93",
        "4_non_latin": "691d4a3aa43c37e7",
        "5_supplementary": "4d93af3ffe99600c",
        "6_comma_quotes": "8871a54e242c993d",
        "7_empty_fields": "9a6c9654149e1444",
        "8_duplicate_revision": "53ee8bf2392f4c12",
    }
    for name, digest in expected_digests.items():
        assert f'CheckDigest("{name}", v{name[0]}, "{digest}")' in MQL5_SELF_TEST_SOURCE or \
               f'CheckDedupDigest("{name}", v8a, v8b, "{digest}")' in MQL5_SELF_TEST_SOURCE, (
            f"DigestSelfTest.mq5 does not check vector {name!r} against the expected digest {digest!r}"
        )


def test_mql5_self_test_declares_the_same_field_values_as_the_python_vectors():
    # Vector 1 (ASCII) field values, verbatim, must appear in the .mq5
    # source -- a byte-level cross-check that the row content itself
    # (not just the final expected digest) was transcribed correctly.
    for field in ("216.000000", "170.000000", "173.000000", "2024.01.05 13:30:00", "2023.12.01"):
        assert field in MQL5_SELF_TEST_SOURCE
    # Non-ASCII vectors' literal text must be present verbatim too.
    assert "Café Price Index" in MQL5_SELF_TEST_SOURCE
    assert "日本銀行" in MQL5_SELF_TEST_SOURCE
    assert "\U0001F600" in MQL5_SELF_TEST_SOURCE
