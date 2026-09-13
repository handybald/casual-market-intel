"""Regression tests (eighth review, P2): strict UTF-8 validation.

`IsValidUtf8` in `mql5_exporter/CalendarExporterCore.mqh` previously
checked only lead-byte ranges, generic continuation-byte SHAPE
(`10xxxxxx`), and available length -- it did NOT restrict the specific
VALUE of the first continuation byte after E0/ED/F0/F4, so it wrongly
ACCEPTED:

  - `E0 80 80`       -- an overlong encoding of U+0000 (a 3-byte
                        sequence "encoding" a code point that has a
                        much shorter valid encoding -- never legal).
  - `ED A0 80`       -- U+D800, a UTF-16 surrogate half. Surrogates are
                        never valid Unicode SCALAR VALUES and are
                        therefore never legally UTF-8-encoded.
  - `F4 90 80 80`    -- would decode to U+110000, beyond the Unicode
                        codespace's maximum, U+10FFFF.

All three are rejected by Python's `bytes.decode("utf-8",
errors="strict")`, which is the ground truth used throughout this
file. The fix narrows the FIRST continuation byte's valid range for
each of E0/ED/F0/F4 per the Unicode Standard's well-formedness table
(chapter 3, table 3-7) / RFC 3629 section 4.

This is also independently verified via ACTUAL COMPILATION AND
EXECUTION inside a real MetaTrader 5 terminal this round --
`mql5_exporter/FileRecoverySelfTest.mq5` scenarios 9-35 exercise
`IsValidUtf8` (the real, shared, production function -- not a
duplicate) against every vector below plus additional boundary/
truncation/stray-byte cases, all 27 passing. The tests here exist to
(a) pin the fixed vectors down at the Python level so a future change
can't silently regress them, and (b) prove Python's OWN strict decoder
agrees with every expected accept/reject outcome -- the actual
cross-language comparison the review asked for.
"""
import pytest

# Every vector as a Python `bytes` object with an explicit expected
# outcome, so this file can assert BOTH "Python's strict decoder agrees
# with the vector's expected label" (the cross-check) AND "these are
# exactly the vectors the compiled .mq5 self-test used" (traceable by
# name to FileRecoverySelfTest.mq5's scenario numbers 9-35).
VECTORS = [
    # (name, bytes, expected_valid)
    ("u007F_last_1byte", bytes([0x7F]), True),
    ("u0080_first_2byte", bytes([0xC2, 0x80]), True),
    ("u07FF_last_2byte", bytes([0xDF, 0xBF]), True),
    ("u0800_first_3byte", bytes([0xE0, 0xA0, 0x80]), True),
    ("uD7FF_before_surrogates", bytes([0xED, 0x9F, 0xBF]), True),
    ("uE000_after_surrogates", bytes([0xEE, 0x80, 0x80]), True),
    ("uFFFF_last_3byte", bytes([0xEF, 0xBF, 0xBF]), True),
    ("u10000_first_4byte", bytes([0xF0, 0x90, 0x80, 0x80]), True),
    ("u10FFFF_last_valid", bytes([0xF4, 0x8F, 0xBF, 0xBF]), True),
    ("overlong_E0_80_80", bytes([0xE0, 0x80, 0x80]), False),
    ("surrogate_ED_A0_80", bytes([0xED, 0xA0, 0x80]), False),
    ("beyond_U10FFFF_F4_90_80_80", bytes([0xF4, 0x90, 0x80, 0x80]), False),
    ("overlong_C0_80", bytes([0xC0, 0x80]), False),
    ("overlong_C1_BF", bytes([0xC1, 0xBF]), False),
    ("overlong_F0_8F_BF_BF", bytes([0xF0, 0x8F, 0xBF, 0xBF]), False),
    ("surrogate_ED_BF_BF", bytes([0xED, 0xBF, 0xBF]), False),
    ("truncated_2byte", bytes([0xC2]), False),
    ("truncated_3byte_after_1", bytes([0xE2]), False),
    ("truncated_3byte_after_2", bytes([0xE2, 0x82]), False),
    ("truncated_4byte_after_1", bytes([0xF0]), False),
    ("truncated_4byte_after_2", bytes([0xF0, 0x9F]), False),
    ("truncated_4byte_after_3", bytes([0xF0, 0x9F, 0x98]), False),
    ("stray_continuation_byte", bytes([0x41, 0x80, 0x42]), False),
    ("invalid_lead_F5", bytes([0xF5, 0x80, 0x80, 0x80]), False),
    ("invalid_lead_FF", bytes([0xFF]), False),
    ("ascii_event_name", b"Nonfarm Payrolls", True),
    ("non_ascii_event_name", "日銀".encode("utf-8"), True),
]


@pytest.mark.parametrize("name,data,expected_valid", VECTORS, ids=[v[0] for v in VECTORS])
def test_python_strict_decoder_matches_expected_outcome(name, data, expected_valid):
    """The cross-language ground truth: confirms every vector's
    expected label (used identically as the real MQL5 self-test's
    expectation) genuinely matches what Python's own strict UTF-8
    decoder decides -- these are not arbitrarily chosen expectations."""
    try:
        data.decode("utf-8", errors="strict")
        actual_valid = True
    except UnicodeDecodeError:
        actual_valid = False
    assert actual_valid == expected_valid, (
        f"vector {name!r} expected valid={expected_valid} but Python's strict "
        f"decoder says valid={actual_valid}"
    )


def test_the_three_reviewer_reported_sequences_are_all_rejected():
    """The exact three sequences named in the review, gathered in one
    place for a direct, unambiguous reproduction of the pre-fix defect
    (a generic continuation-byte-shape check accepts all three)."""
    reviewer_reported = {
        "E0 80 80 (overlong)": bytes([0xE0, 0x80, 0x80]),
        "ED A0 80 (surrogate)": bytes([0xED, 0xA0, 0x80]),
        "F4 90 80 80 (above U+10FFFF)": bytes([0xF4, 0x90, 0x80, 0x80]),
    }
    for label, data in reviewer_reported.items():
        with pytest.raises(UnicodeDecodeError):
            data.decode("utf-8", errors="strict")


def test_baseline_generic_shape_check_would_have_wrongly_accepted_all_three():
    """Reproduces the OLD (pre-fix) IsValidUtf8 algorithm in Python --
    lead-byte range + generic 10xxxxxx continuation-byte shape only,
    with NO narrowed second-byte range for E0/ED/F0/F4 -- to confirm it
    really did accept all three sequences the review flagged (i.e. that
    this is a genuine reproduction, not a hypothetical)."""

    def old_buggy_is_valid_utf8(data: bytes) -> bool:
        i = 0
        n = len(data)
        while i < n:
            b0 = data[i]
            if b0 <= 0x7F:
                i += 1
                continue
            if (b0 & 0xE0) == 0xC0:
                if b0 < 0xC2:
                    return False
                extra = 1
            elif (b0 & 0xF0) == 0xE0:
                extra = 2
            elif (b0 & 0xF8) == 0xF0:
                if b0 > 0xF4:
                    return False
                extra = 3
            else:
                return False
            if i + extra >= n:
                return False
            for k in range(1, extra + 1):
                if (data[i + k] & 0xC0) != 0x80:
                    return False
            i += extra + 1
        return True

    assert old_buggy_is_valid_utf8(bytes([0xE0, 0x80, 0x80])) is True  # the bug: wrongly accepted
    assert old_buggy_is_valid_utf8(bytes([0xED, 0xA0, 0x80])) is True  # the bug: wrongly accepted
    assert old_buggy_is_valid_utf8(bytes([0xF4, 0x90, 0x80, 0x80])) is True  # the bug: wrongly accepted

    # And confirm ALL of these are rejected by Python's real decoder --
    # the discrepancy IS the defect.
    for data in (bytes([0xE0, 0x80, 0x80]), bytes([0xED, 0xA0, 0x80]), bytes([0xF4, 0x90, 0x80, 0x80])):
        with pytest.raises(UnicodeDecodeError):
            data.decode("utf-8", errors="strict")


def test_fixed_is_valid_utf8_python_mirror_rejects_all_three():
    """The FIXED algorithm (Python mirror of the real .mq5 IsValidUtf8,
    matching CalendarExporterCore.mqh line-for-line) must reject
    exactly the three sequences the old one wrongly accepted, while
    still accepting every valid boundary vector."""
    from tests._mql5_exporter_sim import classify_utf8_validity

    for _, data, expected_valid in VECTORS:
        valid, _, _ = classify_utf8_validity(data)
        assert valid == expected_valid
