//+------------------------------------------------------------------+
//|                                    TailRecoverySelfTest.mq5        |
//|                                                                    |
//| Standalone self-test for RecoverFileTail/ComputeSafeAppendBoundary  |
//| /IsValidUtf8Ex -- the interrupted-write recovery machinery added    |
//| for the eighth review's P1 defect (a retry appending onto a         |
//| dangling, non-newline-terminated fragment left by an earlier        |
//| interrupted write, silently merging/losing a record and letting a   |
//| reduced-but-self-consistent digest certify the window OK anyway).   |
//|                                                                    |
//| PRODUCTION HELPERS, NOT DUPLICATES: this file #include's            |
//| CalendarExporterCore.mqh and calls RecoverFileTail,                 |
//| ComputeSafeAppendBoundary, IsValidUtf8Ex, and WriteUtf8Line          |
//| directly -- the SAME compiled functions EconomicCalendarExporter    |
//| .mq5 itself calls from OnStart(). Every scenario below hand-builds  |
//| its synthetic file's bytes at an EXACT record/byte boundary (never  |
//| a generic "the write failed" stand-in) and then re-reads the file    |
//| from disk afterward to assert on the ACTUAL persisted bytes.        |
//|                                                                    |
//| Everything here operates on its own dedicated test files under      |
//| MQL5\Files\, never us_macro_calendar.csv or any real window log.    |
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
      PrintFormat("[TailRecoverySelfTest] PASS: %s (%s)", name, detail);
      g_passed++;
   }
   else
   {
      PrintFormat("[TailRecoverySelfTest] FAIL: %s (%s)", name, detail);
      g_failed++;
   }
}

void WriteRawBytesFresh(string path, uchar &bytes[])
{
   if(FileIsExist(path)) FileDelete(path);
   int h = FileOpen(path, FILE_WRITE | FILE_BIN);
   if(ArraySize(bytes) > 0)
      FileWriteArray(h, bytes, 0, ArraySize(bytes));
   FileClose(h);
}

void AppendUtf8Text(string path, string text)
{
   // Appends WITHOUT a trailing newline -- used to synthesize a
   // dangling fragment exactly as an interrupted WriteUtf8Bytes call
   // (content written, newline byte never reached) would leave one.
   int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
   ulong endPos = FileSize(h);
   FileSeek(h, (long)endPos, SEEK_SET);
   WriteUtf8Bytes(h, text);
   FileClose(h);
}

void AppendRawBytes(string path, uchar &bytes[])
{
   int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
   ulong endPos = FileSize(h);
   FileSeek(h, (long)endPos, SEEK_SET);
   if(ArraySize(bytes) > 0)
      FileWriteArray(h, bytes, 0, ArraySize(bytes));
   FileClose(h);
}

string ReadWhole(string path)
{
   uchar bytes[];
   if(!ReadRawBytes(path, bytes)) return "<<UNREADABLE>>";
   int len = ArraySize(bytes);
   if(len == 0) return "";
   return CharArrayToString(bytes, 0, len, UTF8_CODEPAGE);
}

long FileByteLen(string path)
{
   uchar bytes[];
   if(!ReadRawBytes(path, bytes)) return -1;
   return ArraySize(bytes);
}

string CsvHeaderLine()
{
   return CSV_HEADER;
}

// A complete, valid 16-column CSV row (matches CSV_COLUMN_COUNT).
string CompleteRow(string valueId, string eventName)
{
   return valueId + ",100," + eventName + ",US,USD,HIGH,2024.01.05 13:30:00,2023.12.01,JOB,THOUSANDS,216.000000,170.000000,173.000000,,0,SERVER";
}

// A complete, valid 10-field window-log checkpoint line.
string CompleteLogLine(string tag)
{
   return "2024.01.01 00:00:00,2024.02.01 00:00:00,US,USD,5,OK,3,1,2024.01.31 00:00:00,deadbeef" + tag;
}

void OnStart()
{
   g_passed = 0;
   g_failed = 0;

   //================================================================
   // Scenario 1/required-#1: partial CSV row without a newline, then
   // a retry writing two fresh records -- both must survive.
   //================================================================
   {
      string path = "tailrecovery_csv1.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CsvHeaderLine() + "\n");
      AppendUtf8Text(path, CompleteRow("1", "NFP") + "\n");
      // Interrupted mid-row: only a PARTIAL row (fewer than
      // CSV_COLUMN_COUNT fields, no newline) was written before the
      // process died.
      AppendUtf8Text(path, "2,101,CPI,US,USD");

      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, true);
      Check("1_partial_row_recovery_status", outcome == TAIL_RECOVERY_REPAIRED,
            StringFormat("outcome=%d", (int)outcome));

      string afterRecovery = ReadWhole(path);
      Check("1_row1_preserved", StringFind(afterRecovery, CompleteRow("1", "NFP")) >= 0, "row1 must survive recovery");
      Check("1_partial_row_dropped", StringFind(afterRecovery, "2,101,CPI") < 0, "the dangling partial row must be gone");
      Check("1_ends_with_newline", StringGetCharacter(afterRecovery, StringLen(afterRecovery) - 1) == '\n', "recovered file must end at a clean boundary");

      // Retry: append the two records the retry actually fetches --
      // must both survive, on their own clean lines.
      int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
      ulong endPos = FileSize(h);
      FileSeek(h, (long)endPos, SEEK_SET);
      WriteUtf8Line(h, CompleteRow("2", "CPI"));
      WriteUtf8Line(h, CompleteRow("3", "PPI"));
      FileClose(h);

      string finalContent = ReadWhole(path);
      string lines[];
      int lineCount = SplitUtf8Lines(finalContent, lines);
      int parseable = 0;
      for(int i = 1; i < lineCount; i++)  // skip header
      {
         string fields[];
         if(SplitCsvRow(lines[i], fields)) parseable++;
      }
      Check("1_both_retry_records_survive_and_parse", parseable == 3,
            StringFormat("expected 3 parseable data rows (row1 + 2 retry rows), got %d (total lines=%d)", parseable, lineCount));
      FileDelete(path);
   }

   //================================================================
   // Scenario 2/required-#2: interruption AFTER complete row content
   // but BEFORE the newline -- the record itself is genuinely
   // complete; recovery must NOT drop it, only complete the newline.
   //================================================================
   {
      string path = "tailrecovery_csv2.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CsvHeaderLine() + "\n");
      AppendUtf8Text(path, CompleteRow("1", "NFP") + "\n");
      // Row 2's full content was written, but the interruption landed
      // exactly before its trailing '\n' byte.
      AppendUtf8Text(path, CompleteRow("2", "CPI"));
      long lenBefore = FileByteLen(path);

      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, true);
      Check("2_missing_newline_recovery_status", outcome == TAIL_RECOVERY_REPAIRED,
            StringFormat("outcome=%d", (int)outcome));

      long lenAfter = FileByteLen(path);
      Check("2_exactly_one_byte_added", lenAfter == lenBefore + 1,
            StringFormat("len_before=%d len_after=%d", lenBefore, lenAfter));

      string afterRecovery = ReadWhole(path);
      Check("2_row2_preserved_not_dropped", StringFind(afterRecovery, CompleteRow("2", "CPI")) >= 0,
            "a complete record missing only its newline must never be dropped");

      // Retry: a THIRD record must land on its OWN line, not
      // concatenated onto row2.
      int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
      ulong endPos = FileSize(h);
      FileSeek(h, (long)endPos, SEEK_SET);
      WriteUtf8Line(h, CompleteRow("3", "PPI"));
      FileClose(h);

      string finalContent = ReadWhole(path);
      Check("2_no_concatenation_with_next_record",
            StringFind(finalContent, "SERVER3,101") < 0 && StringFind(finalContent, ",SERVER3,") < 0,
            "row2 and row3 must never merge into one token");
      string lines[];
      int lineCount = SplitUtf8Lines(finalContent, lines);
      int parseable = 0;
      for(int i = 1; i < lineCount; i++)
      {
         string fields[];
         if(SplitCsvRow(lines[i], fields)) parseable++;
      }
      Check("2_all_three_records_independently_parseable", parseable == 3,
            StringFormat("parseable=%d total_lines=%d", parseable, lineCount));
      FileDelete(path);
   }

   //================================================================
   // Scenario 3/required-#3: interruption INSIDE a quoted field --
   // the row ends with an opened-but-never-closed quote. Must be
   // treated as INCOMPLETE (dropped), never as a coincidentally-
   // complete row.
   //================================================================
   {
      string path = "tailrecovery_csv3.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CsvHeaderLine() + "\n");
      AppendUtf8Text(path, CompleteRow("1", "NFP") + "\n");
      // A row whose event_name field opens a quote (because the real
      // name contains a comma) and is cut off mid-field -- no closing
      // quote, no newline.
      AppendUtf8Text(path, "2,101,\"Fed Says, Rates Steady and");

      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, true);
      Check("3_unterminated_quote_recovery_status", outcome == TAIL_RECOVERY_REPAIRED,
            StringFormat("outcome=%d", (int)outcome));

      string afterRecovery = ReadWhole(path);
      Check("3_unterminated_quoted_row_dropped", StringFind(afterRecovery, "Fed Says") < 0,
            "a row interrupted mid-quoted-field must never be treated as complete");
      Check("3_row1_preserved", StringFind(afterRecovery, CompleteRow("1", "NFP")) >= 0, "row1 must survive");
      Check("3_ends_with_newline", StringGetCharacter(afterRecovery, StringLen(afterRecovery) - 1) == '\n', "must end cleanly");
      FileDelete(path);
   }

   //================================================================
   // Scenario 4/required-#4: interruption INSIDE a multibyte UTF-8
   // sequence -- the row's last byte is a lead byte promising more
   // continuation bytes that never arrived.
   //================================================================
   {
      string path = "tailrecovery_csv4.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CsvHeaderLine() + "\n");
      AppendUtf8Text(path, CompleteRow("1", "NFP") + "\n");
      // Complete ASCII prefix, then a lone 3-byte-sequence lead byte
      // (0xE6, as in a CJK character) with ZERO continuation bytes --
      // exactly what an interruption mid-character leaves behind.
      string prefix = "2,101,Bo";
      uchar prefixBytes[];
      int n = StringToCharArray(prefix, prefixBytes, 0, -1, UTF8_CODEPAGE);
      int prefixLen = (n > 0) ? n - 1 : 0;
      uchar tailBytes[];
      ArrayResize(tailBytes, prefixLen + 1);
      for(int i = 0; i < prefixLen; i++) tailBytes[i] = prefixBytes[i];
      tailBytes[prefixLen] = 0xE6;  // truncated lead byte -- no continuation bytes follow
      AppendRawBytes(path, tailBytes);

      long lenBeforeRecovery = FileByteLen(path);
      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, true);
      Check("4_truncated_utf8_recovery_status", outcome == TAIL_RECOVERY_REPAIRED,
            StringFormat("outcome=%d", (int)outcome));

      long lenAfterRecovery = FileByteLen(path);
      Check("4_dropped_exactly_the_incomplete_tail",
            lenAfterRecovery < lenBeforeRecovery,
            StringFormat("len_before=%d len_after=%d (must shrink -- the truncated char can never be completed by guessing)",
                         lenBeforeRecovery, lenAfterRecovery));

      uchar finalBytes[];
      ReadRawBytes(path, finalBytes);
      Check("4_final_bytes_still_valid_utf8", IsValidUtf8(finalBytes, ArraySize(finalBytes)),
            "no silent corruption -- the recovered file itself must remain valid UTF-8");
      string afterRecovery = CharArrayToString(finalBytes, 0, ArraySize(finalBytes), UTF8_CODEPAGE);
      Check("4_row1_preserved", StringFind(afterRecovery, CompleteRow("1", "NFP")) >= 0, "row1 must survive");
      FileDelete(path);
   }

   //================================================================
   // Scenario 5/required-#5: partial checkpoint (window-log) line,
   // then a new checkpoint -- earlier valid entries survive and the
   // new entry is independently parseable.
   //================================================================
   {
      string path = "tailrecovery_log1.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CompleteLogLine("aaaa1111") + "\n");
      // Interrupted mid-write of a second checkpoint line: only 4 of
      // the 10 comma-separated fields made it out before the process
      // died, no newline.
      AppendUtf8Text(path, "2024.02.01 00:00:00,2024.03.01 00:00:00,US,USD");

      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, false);
      Check("5_partial_checkpoint_recovery_status", outcome == TAIL_RECOVERY_REPAIRED,
            StringFormat("outcome=%d", (int)outcome));

      string afterRecovery = ReadWhole(path);
      Check("5_first_checkpoint_survives", StringFind(afterRecovery, "aaaa1111") >= 0, "earlier valid entry must survive");
      Check("5_partial_checkpoint_dropped", StringFind(afterRecovery, "2024.03.01") < 0, "the dangling partial checkpoint must be gone");

      // A genuinely new checkpoint appended after recovery must be
      // independently parseable (exactly 10 fields), not merged with
      // anything.
      int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
      ulong endPos = FileSize(h);
      FileSeek(h, (long)endPos, SEEK_SET);
      WriteUtf8Line(h, CompleteLogLine("bbbb2222"));
      FileClose(h);

      string finalContent = ReadWhole(path);
      string lines[];
      int lineCount = SplitUtf8Lines(finalContent, lines);
      Check("5_exactly_two_lines", lineCount == 2, StringFormat("lineCount=%d", lineCount));
      int parseableCount = 0;
      for(int i = 0; i < lineCount; i++)
      {
         string parts[];
         if(StringSplit(lines[i], ',', parts) == 10) parseableCount++;
      }
      Check("5_both_lines_independently_parseable", parseableCount == 2,
            StringFormat("parseableCount=%d", parseableCount));
      FileDelete(path);
   }

   //================================================================
   // Scenario 6: a checkpoint's complete-but-newline-missing last line
   // (the log-format equivalent of scenario 2) must also be preserved,
   // not dropped.
   //================================================================
   {
      string path = "tailrecovery_log2.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CompleteLogLine("cccc3333"));  // complete, but no trailing newline at all

      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, false);
      Check("6_checkpoint_missing_newline_repaired", outcome == TAIL_RECOVERY_REPAIRED,
            StringFormat("outcome=%d", (int)outcome));
      string afterRecovery = ReadWhole(path);
      Check("6_checkpoint_preserved", StringFind(afterRecovery, "cccc3333") >= 0, "must not be dropped");
      Check("6_ends_with_newline", StringGetCharacter(afterRecovery, StringLen(afterRecovery) - 1) == '\n', "must end cleanly");
      FileDelete(path);
   }

   //================================================================
   // Scenario 7: an already-clean file (ends at a newline boundary)
   // must be reported CLEAN and left byte-for-byte unchanged.
   //================================================================
   {
      string path = "tailrecovery_clean.csv";
      uchar empty[];
      WriteRawBytesFresh(path, empty);
      AppendUtf8Text(path, CsvHeaderLine() + "\n");
      AppendUtf8Text(path, CompleteRow("1", "NFP") + "\n");
      long lenBefore = FileByteLen(path);
      string contentBefore = ReadWhole(path);

      ENUM_TAIL_RECOVERY outcome = RecoverFileTail(path, true);
      Check("7_clean_file_reports_clean", outcome == TAIL_RECOVERY_CLEAN, StringFormat("outcome=%d", (int)outcome));
      Check("7_clean_file_byte_identical", FileByteLen(path) == lenBefore && ReadWhole(path) == contentBefore,
            "a clean file must never be touched");
      FileDelete(path);
   }

   //================================================================
   // Scenario 8: a missing file and an empty file both report CLEAN
   // (nothing to recover).
   //================================================================
   {
      string missingPath = "tailrecovery_missing.csv";
      if(FileIsExist(missingPath)) FileDelete(missingPath);
      Check("8_missing_file_is_clean", RecoverFileTail(missingPath, true) == TAIL_RECOVERY_CLEAN, "no file -- nothing to recover");

      string emptyPath = "tailrecovery_empty.csv";
      uchar empty[];
      WriteRawBytesFresh(emptyPath, empty);
      Check("8_empty_file_is_clean", RecoverFileTail(emptyPath, true) == TAIL_RECOVERY_CLEAN, "zero-byte file -- nothing to recover");
      FileDelete(emptyPath);
   }

   //================================================================
   // Scenario 9: IsValidUtf8Ex correctly distinguishes "truncated at
   // buffer end" (recoverable interruption) from other invalidity --
   // this is the exact primitive MigrateLegacyCsvIfNeeded's
   // truncation-tolerant check depends on, so a whole otherwise-valid
   // history is never wrongly migrated aside just because the very
   // last write was cut off mid-character.
   //================================================================
   {
      string prefix = CsvHeaderLine() + "\n" + CompleteRow("1", "NFP") + "\n";
      uchar prefixBytes[];
      int n = StringToCharArray(prefix, prefixBytes, 0, -1, UTF8_CODEPAGE);
      int prefixLen = (n > 0) ? n - 1 : 0;
      uchar whole[];
      ArrayResize(whole, prefixLen + 1);
      for(int i = 0; i < prefixLen; i++) whole[i] = prefixBytes[i];
      whole[prefixLen] = 0xF0;  // truncated 4-byte lead, no continuation bytes at all

      bool truncatedAtEnd;
      int validPrefixLen;
      bool wholeValid = IsValidUtf8Ex(whole, ArraySize(whole), truncatedAtEnd, validPrefixLen);
      Check("9_truncated_tail_reported_as_invalid_overall", !wholeValid, "the whole buffer is not valid UTF-8 as-is");
      Check("9_truncated_tail_flagged_as_truncation", truncatedAtEnd, "must be classified as a recoverable trailing truncation");
      Check("9_valid_prefix_length_correct", validPrefixLen == prefixLen,
            StringFormat("expected_prefix_len=%d actual=%d", prefixLen, validPrefixLen));

      // Contrast: a genuinely non-UTF-8 byte with PLENTY of bytes after
      // it (not a truncation -- e.g. a real ANSI file) must NOT be
      // flagged as a tolerable truncation.
      uchar ansiLike[7] = {'C', 'a', 'f', 0xE9, 'X', 'Y', 'Z'};  // lone high byte, then MORE bytes follow
      bool truncatedAtEnd2;
      int validPrefixLen2;
      bool wholeValid2 = IsValidUtf8Ex(ansiLike, 7, truncatedAtEnd2, validPrefixLen2);
      Check("9_non_truncation_invalidity_not_misclassified", !wholeValid2 && !truncatedAtEnd2,
            "a lone invalid byte followed by more bytes is NOT a truncation -- must not be tolerated as one");
   }

   PrintFormat("[TailRecoverySelfTest] ==== %d/%d scenarios passed ====", g_passed, g_passed + g_failed);
}
//+------------------------------------------------------------------+
