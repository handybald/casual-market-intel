//+------------------------------------------------------------------+
//|                                    FileRecoverySelfTest.mq5        |
//|                                                                    |
//| Standalone self-test for the durable-append (defect A), UTF-8      |
//| migration-validation (defect C), and STRICT UTF-8 validation        |
//| (eighth review, P2) behavior in EconomicCalendarExporter.mq5.       |
//|                                                                    |
//| PRODUCTION HELPERS, NOT DUPLICATES (eighth review): this file       |
//| #include's CalendarExporterCore.mqh and calls ReadRawBytes,         |
//| IsValidUtf8, WriteUtf8Bytes, and WriteUtf8Line directly -- the SAME  |
//| compiled functions the real exporter uses, not a hand-typed copy    |
//| that could pass while the production code stayed wrong. Everything  |
//| here operates on its OWN dedicated test files under MQL5\Files\,    |
//| never us_macro_calendar.csv.                                        |
//|                                                                    |
//| Prints PASS/FAIL per scenario and a final summary to the           |
//| Experts/Journal tab.                                                |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

#include "CalendarExporterCore.mqh"

int g_passed = 0;
int g_failed = 0;

void Check(string name, bool condition, string detail)
{
   if(condition)
   {
      PrintFormat("[FileRecoverySelfTest] PASS: %s (%s)", name, detail);
      g_passed++;
   }
   else
   {
      PrintFormat("[FileRecoverySelfTest] FAIL: %s (%s)", name, detail);
      g_failed++;
   }
}

// Durable-append line write -- the FIXED LogWindowStatus pattern
// (FILE_READ|FILE_WRITE, never bare FILE_WRITE, explicit seek-to-end
// checked before writing), built from the SHARED WriteUtf8Line/
// FileSeek/FileOpen -- only the open/seek/write ORDERING is
// reconstructed here (LogWindowStatus itself lives in the main
// exporter file and depends on its script-level globals, so it can't
// be #include'd directly), not the byte-level logic under test.
bool AppendLineDurable(string path, string line)
{
   int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
   if(h == INVALID_HANDLE) return false;
   ulong endPos = FileSize(h);
   if(!FileSeek(h, (long)endPos, SEEK_SET))
   {
      FileClose(h);
      return false;
   }
   bool ok = WriteUtf8Line(h, line);
   if(ok) FileFlush(h);
   FileClose(h);
   return ok;
}

string ReadWholeFileUtf8(string path)
{
   uchar bytes[];
   if(!ReadRawBytes(path, bytes)) return "<<UNREADABLE>>";
   int len = ArraySize(bytes);
   if(len == 0) return "";
   return CharArrayToString(bytes, 0, len, UTF8_CODEPAGE);
}

// Builds a uchar[] from explicit byte values (as ints, 0-255) -- used
// throughout the strict-UTF-8 vectors below so exact byte sequences
// (including invalid ones no string literal could represent) are
// unambiguous.
void MakeBytes(int &values[], uchar &outBytes[])
{
   int n = ArraySize(values);
   ArrayResize(outBytes, n);
   for(int i = 0; i < n; i++)
      outBytes[i] = (uchar)values[i];
}

void CheckUtf8(string name, int &byteValues[], bool expectedValid)
{
   uchar bytes[];
   MakeBytes(byteValues, bytes);
   bool actual = IsValidUtf8(bytes, ArraySize(bytes));
   Check(name, actual == expectedValid,
         StringFormat("expected_valid=%s actual_valid=%s byte_count=%d", expectedValid ? "true" : "false",
                      actual ? "true" : "false", ArraySize(bytes)));
}

void OnStart()
{
   g_passed = 0;
   g_failed = 0;
   string path = "recovery_selftest.log";

   // -- Scenario 1: durable append preserves prior content across
   // multiple calls (defect A) --
   if(FileIsExist(path)) FileDelete(path);
   AppendLineDurable(path, "line1");
   AppendLineDurable(path, "line2");
   AppendLineDurable(path, "line3");
   string content1 = ReadWholeFileUtf8(path);
   Check("1_append_preserves_all_three_lines",
         StringFind(content1, "line1") >= 0 && StringFind(content1, "line2") >= 0 && StringFind(content1, "line3") >= 0,
         StringFormat("content=[%s]", content1));

   // -- Scenario 2: interrupting BEFORE the append (simulated by simply
   // never calling AppendLineDurable) must leave prior content intact --
   // trivially true by construction, but confirm the file size only
   // grew by exactly the 3 appended lines' worth, never more/less due
   // to any truncation side effect. --
   int expectedApprox = StringLen("line1\nline2\nline3\n");
   Check("2_no_extra_truncation_or_duplication",
         StringLen(content1) == expectedApprox,
         StringFormat("expected_len=%d actual_len=%d", expectedApprox, StringLen(content1)));

   // -- Scenario 3: a bare FILE_WRITE (the OLD buggy pattern) DOES
   // destroy prior content -- documents the exact defect this fixes,
   // rather than just asserting the new function is safe in isolation. --
   int hOld = FileOpen(path, FILE_WRITE | FILE_BIN);
   FileClose(hOld);
   string contentAfterBareWrite = ReadWholeFileUtf8(path);
   Check("3_bare_FILE_WRITE_would_have_destroyed_history (documents the defect)",
         StringLen(contentAfterBareWrite) == 0,
         StringFormat("content_after_bare_FILE_WRITE_open=[%s] (0 length confirms the original bug's mechanism)", contentAfterBareWrite));

   // -- Scenario 4: IsValidUtf8 on genuine UTF-8 bytes (ASCII + a
   // 3-byte non-Latin sequence) must accept. --
   string csvPathValid = "recovery_selftest_valid.csv";
   if(FileIsExist(csvPathValid)) FileDelete(csvPathValid);
   int hv = FileOpen(csvPathValid, FILE_WRITE | FILE_BIN);
   WriteUtf8Line(hv, "value_id,event_name");
   WriteUtf8Line(hv, "1,日本銀行");
   FileClose(hv);
   uchar validBytes[];
   ReadRawBytes(csvPathValid, validBytes);
   Check("4_valid_utf8_csv_accepted", IsValidUtf8(validBytes, ArraySize(validBytes)),
         StringFormat("byte_len=%d", ArraySize(validBytes)));

   // -- Scenario 5: a synthetic "legacy ANSI" file containing a raw
   // high byte (0xE9, "e-acute" in Windows-1252) that is NOT a valid
   // standalone UTF-8 sequence must be REJECTED (defect C's core case:
   // pure-ASCII header would otherwise look identical to a real v5 file). --
   string csvPathAnsi = "recovery_selftest_ansi.csv";
   if(FileIsExist(csvPathAnsi)) FileDelete(csvPathAnsi);
   int ha = FileOpen(csvPathAnsi, FILE_WRITE | FILE_BIN);
   uchar ansiHeader[];
   StringToCharArray("value_id,event_name", ansiHeader, 0, -1, UTF8_CODEPAGE); // header itself is pure ASCII
   int ansiHeaderLen = ArraySize(ansiHeader) - 1;
   FileWriteArray(ha, ansiHeader, 0, ansiHeaderLen);
   uchar nl[1]; nl[0] = (uchar)'\n';
   FileWriteArray(ha, nl, 0, 1);
   uchar ansiRow[7] = {'1', ',', 'C', 'a', 'f', 0xE9, '\n'};  // "1,Caf<0xE9>" -- lone high byte, invalid UTF-8
   FileWriteArray(ha, ansiRow, 0, 7);
   FileClose(ha);
   uchar ansiBytes[];
   ReadRawBytes(csvPathAnsi, ansiBytes);
   Check("5_ansi_encoded_lone_high_byte_rejected", !IsValidUtf8(ansiBytes, ArraySize(ansiBytes)),
         StringFormat("byte_len=%d (must be rejected despite pure-ASCII header)", ArraySize(ansiBytes)));

   // -- Scenario 6: legacy pure-ASCII file (no non-ASCII bytes at all)
   // -- ambiguous in principle (ANSI and UTF-8 are byte-identical for
   // ASCII), but per the documented design decision this is treated as
   // VALID UTF-8 (safe: ASCII bytes mean the same thing under both
   // encodings), so pure-ASCII legacy content is accepted. --
   string csvPathAscii = "recovery_selftest_ascii.csv";
   if(FileIsExist(csvPathAscii)) FileDelete(csvPathAscii);
   int hascii = FileOpen(csvPathAscii, FILE_WRITE | FILE_BIN);
   WriteUtf8Line(hascii, "value_id,event_name");
   WriteUtf8Line(hascii, "1,NFP");
   FileClose(hascii);
   uchar asciiBytes[];
   ReadRawBytes(csvPathAscii, asciiBytes);
   Check("6_pure_ascii_legacy_file_accepted", IsValidUtf8(asciiBytes, ArraySize(asciiBytes)),
         StringFormat("byte_len=%d", ArraySize(asciiBytes)));

   // -- Scenario 7: malformed/truncated UTF-8 (a lead byte promising a
   // 3-byte sequence with only 1 continuation byte available) must be
   // rejected. --
   uchar truncated[3] = {0xE2, 0x82, 0x00};  // would-be 3-byte sequence, last byte 0x00 is not a continuation byte
   Check("7_malformed_truncated_utf8_rejected", !IsValidUtf8(truncated, 3), "synthetic truncated 3-byte sequence");

   // -- Scenario 8: missing file vs. corrupt/unreadable file are
   // distinguishable -- ReadRawBytes returns false for a nonexistent
   // path (checked via FileIsExist first), which is the "missing"
   // signal; an existing-but-empty file returns true with a 0-length
   // array (not an error). --
   string missingPath = "recovery_selftest_does_not_exist.csv";
   if(FileIsExist(missingPath)) FileDelete(missingPath);
   uchar dummy[];
   bool missingOk = ReadRawBytes(missingPath, dummy);
   Check("8_missing_file_reported_distinctly", missingOk == false, "ReadRawBytes returns false for a nonexistent path");

   // ==================================================================
   // Scenarios 9+: STRICT UTF-8 validation (eighth review, P2).
   //
   // The reviewer reproduced the PREVIOUS IsValidUtf8 (generic
   // continuation-byte-shape-only check) wrongly ACCEPTING three
   // sequences that Python's `bytes.decode("utf-8", errors="strict")`
   // rejects: E0 80 80 (overlong), ED A0 80 (UTF-16 surrogate), F4 90
   // 80 80 (beyond U+10FFFF). Every vector below was cross-checked
   // against Python's strict decoder (see
   // tests/test_mql5_strict_utf8_validation.py, which encodes/decodes
   // the SAME code points independently and asserts IsValidUtf8's
   // Python mirror agrees) before being hardcoded here.
   // ==================================================================

   // -- Boundary pairs: one code point below a length-class boundary,
   // one at/above it -- both must be ACCEPTED (these are all valid). --
   int b007F[] = {0x7F};                               // U+007F -- last 1-byte code point
   CheckUtf8("9_u007F_last_1byte", b007F, true);
   int b0080[] = {0xC2, 0x80};                          // U+0080 -- first 2-byte code point
   CheckUtf8("10_u0080_first_2byte", b0080, true);
   int b07FF[] = {0xDF, 0xBF};                          // U+07FF -- last 2-byte code point
   CheckUtf8("11_u07FF_last_2byte", b07FF, true);
   int b0800[] = {0xE0, 0xA0, 0x80};                    // U+0800 -- first 3-byte code point (E0's narrowed range)
   CheckUtf8("12_u0800_first_3byte", b0800, true);
   int bD7FF[] = {0xED, 0x9F, 0xBF};                    // U+D7FF -- last code point before the surrogate range
   CheckUtf8("13_uD7FF_before_surrogates", bD7FF, true);
   int bE000[] = {0xEE, 0x80, 0x80};                    // U+E000 -- first code point after the surrogate range
   CheckUtf8("14_uE000_after_surrogates", bE000, true);
   int bFFFF[] = {0xEF, 0xBF, 0xBF};                    // U+FFFF -- last 3-byte code point
   CheckUtf8("15_uFFFF_last_3byte", bFFFF, true);
   int b10000[] = {0xF0, 0x90, 0x80, 0x80};             // U+10000 -- first 4-byte code point (F0's narrowed range)
   CheckUtf8("16_u10000_first_4byte", b10000, true);
   int b10FFFF[] = {0xF4, 0x8F, 0xBF, 0xBF};            // U+10FFFF -- last valid Unicode code point
   CheckUtf8("17_u10FFFF_last_valid", b10FFFF, true);

   // -- The three specific invalid sequences named in the review --
   int overlong3[] = {0xE0, 0x80, 0x80};                // overlong encoding of U+0000
   CheckUtf8("18_overlong_E0_80_80_rejected", overlong3, false);
   int surrogate[] = {0xED, 0xA0, 0x80};                // U+D800 -- a UTF-16 surrogate half, never valid UTF-8
   CheckUtf8("19_surrogate_ED_A0_80_rejected", surrogate, false);
   int beyondMax[] = {0xF4, 0x90, 0x80, 0x80};          // would be U+110000 -- beyond U+10FFFF
   CheckUtf8("20_beyond_U10FFFF_F4_90_80_80_rejected", beyondMax, false);

   // -- Additional overlong/out-of-range vectors for the SAME narrowed-
   // second-byte rule, at the other length classes --
   int overlong2[] = {0xC0, 0x80};                      // overlong encoding of U+0000 via a 2-byte sequence
   CheckUtf8("21_overlong_C0_80_rejected", overlong2, false);
   int overlong2b[] = {0xC1, 0xBF};                     // overlong -- C1 is always invalid
   CheckUtf8("22_overlong_C1_BF_rejected", overlong2b, false);
   int overlong4[] = {0xF0, 0x8F, 0xBF, 0xBF};          // overlong encoding via a 4-byte sequence (F0 second byte < 0x90)
   CheckUtf8("23_overlong_F0_8F_BF_BF_rejected", overlong4, false);
   int surrogateHigh[] = {0xED, 0xBF, 0xBF};            // U+DFFF -- last surrogate half
   CheckUtf8("24_surrogate_ED_BF_BF_rejected", surrogateHigh, false);

   // -- Truncated sequences of each length (lead byte correct, but the
   // buffer ends before enough continuation bytes are present) --
   int trunc2[] = {0xC2};
   CheckUtf8("25_truncated_2byte_rejected", trunc2, false);
   int trunc3a[] = {0xE2};
   CheckUtf8("26_truncated_3byte_after_1_rejected", trunc3a, false);
   int trunc3b[] = {0xE2, 0x82};
   CheckUtf8("27_truncated_3byte_after_2_rejected", trunc3b, false);
   int trunc4a[] = {0xF0};
   CheckUtf8("28_truncated_4byte_after_1_rejected", trunc4a, false);
   int trunc4b[] = {0xF0, 0x9F};
   CheckUtf8("29_truncated_4byte_after_2_rejected", trunc4b, false);
   int trunc4c[] = {0xF0, 0x9F, 0x98};
   CheckUtf8("30_truncated_4byte_after_3_rejected", trunc4c, false);

   // -- Stray continuation bytes and invalid lead bytes --
   int strayCont[] = {0x41, 0x80, 0x42};                // 'A', a lone continuation byte, 'B'
   CheckUtf8("31_stray_continuation_byte_rejected", strayCont, false);
   int invalidLeadF5[] = {0xF5, 0x80, 0x80, 0x80};      // F5-FF are never valid lead bytes
   CheckUtf8("32_invalid_lead_F5_rejected", invalidLeadF5, false);
   int invalidLeadFF[] = {0xFF};
   CheckUtf8("33_invalid_lead_FF_rejected", invalidLeadFF, false);

   // -- Plain ASCII and a representative real calendar event name
   // (mixed ASCII + 3-byte non-Latin text) must be accepted whole. --
   int asciiName[] = {'N', 'o', 'n', 'f', 'a', 'r', 'm', ' ', 'P', 'a', 'y', 'r', 'o', 'l', 'l', 's'};
   CheckUtf8("34_ascii_event_name_accepted", asciiName, true);
   // "日銀" (Bank of Japan, abbreviated) -- two 3-byte CJK characters back-to-back.
   int nonAsciiName[] = {0xE6, 0x97, 0xA5, 0xE9, 0x8A, 0x80};
   CheckUtf8("35_non_ascii_event_name_accepted", nonAsciiName, true);

   // cleanup
   FileDelete(path);
   FileDelete(csvPathValid);
   FileDelete(csvPathAnsi);
   FileDelete(csvPathAscii);

   PrintFormat("[FileRecoverySelfTest] ==== %d/%d scenarios passed ====", g_passed, g_passed + g_failed);
}
//+------------------------------------------------------------------+
