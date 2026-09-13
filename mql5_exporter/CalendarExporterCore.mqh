//+------------------------------------------------------------------+
//|                                    CalendarExporterCore.mqh        |
//|                                                                    |
//| Shared, script-input-INDEPENDENT helpers used by                   |
//| EconomicCalendarExporter.mq5: UTF-8 binary file I/O, strict UTF-8   |
//| validation, quote-aware CSV row splitting, the FNV-1a canonical-    |
//| record digest, and interrupted-write tail recovery.                |
//|                                                                    |
//| WHY THIS FILE EXISTS (eighth review): a prior round of self-tests  |
//| (FileRecoverySelfTest.mq5, DigestSelfTest.mq5) duplicated these     |
//| function bodies rather than calling the exporter's own code, so a   |
//| passing self-test could not actually prove the PRODUCTION           |
//| implementation was correct -- only that an independently-retyped    |
//| copy of it was. Every self-test script in this directory now        |
//| #include's THIS file, and so does EconomicCalendarExporter.mq5      |
//| itself -- there is exactly one implementation of each function      |
//| below, compiled into both the real exporter and its tests.          |
//|                                                                    |
//| Deliberately NOT moved here: MigrateLegacyCsvIfNeeded,               |
//| LogWindowStatus, RescanWindowDigest, WindowAlreadyOk,                |
//| FetchMonthWindow, OnStart -- these depend on the script's `input`    |
//| variables (OutputFile, CountryCode, ...) and/or the live Calendar    |
//| API, so pulling them in here would either force every test to        |
//| declare fake globals or reach for a live terminal. Keeping this      |
//| file input-free and API-free is what makes it usable, unmodified,   |
//| from a small standalone test script.                                |
//+------------------------------------------------------------------+
#ifndef CALENDAR_EXPORTER_CORE_MQH
#define CALENDAR_EXPORTER_CORE_MQH

#define UTF8_CODEPAGE 65001
#define CSV_HEADER "value_id,event_id,event_name,country_code,currency_code,importance,event_time,period,unit,multiplier,actual_value,forecast_value,prev_value,revised_prev_value,revision,source_timezone"
#define CSV_COLUMN_COUNT 16

//+------------------------------------------------------------------+
//| UTF-8 BINARY FILE I/O                                              |
//+------------------------------------------------------------------+

// Converts `text` to UTF-8 bytes and writes them (with NO added
// newline) to an already-open FILE_BIN handle. Checks FileWriteArray's
// documented return value rather than assuming success.
bool WriteUtf8Bytes(int fileHandle, string text)
{
   uchar bytes[];
   int n = StringToCharArray(text, bytes, 0, -1, UTF8_CODEPAGE);
   int len = (n > 0) ? n - 1 : 0;  // exclude the trailing NUL terminator
   if(len <= 0)
      return true;  // nothing to write is not a failure
   uint written = FileWriteArray(fileHandle, bytes, 0, len);
   return ((int)written == len);
}

// WriteUtf8Bytes followed by exactly one '\n' byte. Both writes'
// return values are checked -- if this returns false after the content
// bytes succeeded but the newline byte did not, the file is left with
// a dangling, non-newline-terminated fragment; see RecoverFileTail for
// how a LATER run detects and repairs exactly that state.
bool WriteUtf8Line(int fileHandle, string text)
{
   if(!WriteUtf8Bytes(fileHandle, text))
      return false;
   uchar newline[1];
   newline[0] = (uchar)'\n';
   uint written = FileWriteArray(fileHandle, newline, 0, 1);
   return (written == 1);
}

// Reads the raw bytes of `path` into `outBytes`. Returns false (and an
// empty array) if the file doesn't exist, can't be opened, or a short
// read occurs -- callers can distinguish "missing" (check FileIsExist
// first) from "exists but unreadable/corrupt" (this returning false
// despite the file existing).
bool ReadRawBytes(string path, uchar &outBytes[])
{
   ArrayResize(outBytes, 0);
   if(!FileIsExist(path))
      return false;
   int h = FileOpen(path, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE)
      return false;
   ulong size = FileSize(h);
   if(size == 0)
   {
      FileClose(h);
      return true;
   }
   ArrayResize(outBytes, (int)size);
   uint readCount = FileReadArray(h, outBytes, 0, (int)size);
   FileClose(h);
   if((ulong)readCount != size)
   {
      ArrayResize(outBytes, 0);
      return false;
   }
   return true;
}

// Validates that bytes[0..len) is STRICT well-formed UTF-8 (RFC 3629 /
// the Unicode Standard's well-formedness table -- the same table used
// by Python's `bytes.decode("utf-8", errors="strict")`): ASCII bytes
// pass trivially; a multi-byte sequence's lead byte determines its
// length; EVERY continuation byte must match 10xxxxxx; and, beyond
// that generic shape check, the FIRST continuation byte after certain
// lead bytes has an additionally NARROWED valid range, needed to
// reject:
//   - overlong encodings (E0 80..9F .. and F0 80..8F ..) -- a shorter
//     encoding exists for the same code point, e.g. E0 80 80 would
//     "encode" U+0000, which MUST be encoded as a single 00 byte;
//   - UTF-16 surrogate code points U+D800-U+DFFF (ED A0..BF ..) -- not
//     valid Unicode scalar values, never legally UTF-8-encoded;
//   - code points beyond U+10FFFF (F4 90..BF .. and F5-F7 xx ..) --
//     outside the Unicode codespace entirely.
// A generic "every continuation byte in 80-BF" check (the previous
// version of this function) accepts all three of the above -- they
// have the right SHAPE but decode to a value the standard forbids.
// See the Unicode Standard chapter 3 "Conformance", table 3-7
// ("Well-Formed UTF-8 Byte Sequences"), and RFC 3629 section 4.
// Full result form: on any invalid byte, `outValidPrefixLen` reports
// how many LEADING bytes were confirmed valid before the problem (0 if
// the very first byte is already bad), and `outTruncatedAtEnd` is true
// ONLY when the sole problem is the buffer simply running out of bytes
// partway through an otherwise well-formed final sequence (a
// consequence of an interrupted write, distinct from a lead/
// continuation byte that is actively wrong) -- see MigrateLegacyCsv-
// IfNeeded, which uses this distinction so a file that is entirely
// valid UTF-8 except for one truncated trailing sequence is treated as
// a recoverable interruption (repaired by RecoverFileTail right after)
// rather than a wholesale encoding-incompatible legacy file.
bool IsValidUtf8Ex(const uchar &bytes[], int len, bool &outTruncatedAtEnd, int &outValidPrefixLen)
{
   outTruncatedAtEnd = false;
   outValidPrefixLen = len;
   int i = 0;
   while(i < len)
   {
      uchar b0 = bytes[i];
      if(b0 <= 0x7F) { i += 1; continue; }  // ASCII

      int extra;
      uchar minSecond = 0x80, maxSecond = 0xBF;  // default continuation-byte range

      if((b0 & 0xE0) == 0xC0)
      {
         if(b0 < 0xC2) { outValidPrefixLen = i; return false; }  // overlong 2-byte encoding (C0/C1)
         extra = 1;
      }
      else if((b0 & 0xF0) == 0xE0)
      {
         extra = 2;
         if(b0 == 0xE0)      { minSecond = 0xA0; maxSecond = 0xBF; }  // reject overlong (would encode < U+0800)
         else if(b0 == 0xED) { minSecond = 0x80; maxSecond = 0x9F; }  // reject surrogate range U+D800-U+DFFF
      }
      else if((b0 & 0xF8) == 0xF0)
      {
         if(b0 > 0xF4) { outValidPrefixLen = i; return false; }  // lead byte alone already implies > U+10FFFF (or 0xF8-0xFF, invalid)
         extra = 3;
         if(b0 == 0xF0)      { minSecond = 0x90; maxSecond = 0xBF; }  // reject overlong (would encode < U+10000)
         else if(b0 == 0xF4) { minSecond = 0x80; maxSecond = 0x8F; }  // reject > U+10FFFF
      }
      else
      {
         outValidPrefixLen = i;
         return false;  // invalid lead byte (stray continuation byte, or 0xF8-0xFF)
      }

      if(i + extra >= len)
      {
         outTruncatedAtEnd = true;  // otherwise-valid lead byte, just ran out of buffer
         outValidPrefixLen = i;
         return false;
      }

      uchar b1 = bytes[i + 1];
      if(b1 < minSecond || b1 > maxSecond)
      {
         outValidPrefixLen = i;
         return false;  // first continuation byte outside its (possibly narrowed) valid range
      }

      for(int k = 2; k <= extra; k++)
      {
         if((bytes[i + k] & 0xC0) != 0x80)
         {
            outValidPrefixLen = i;
            return false;  // not a continuation byte
         }
      }
      i += (extra + 1);
   }
   return true;
}

bool IsValidUtf8(const uchar &bytes[], int len)
{
   bool truncatedAtEnd;
   int validPrefixLen;
   return IsValidUtf8Ex(bytes, len, truncatedAtEnd, validPrefixLen);
}

// Reads the ENTIRE file at `path` as UTF-8 bytes and decodes it into
// `outContent`. Returns false (outContent set to "") on any failure --
// missing file, open failure, short read, OR bytes that are not
// strictly valid UTF-8 (checked via IsValidUtf8 BEFORE ever calling
// CharArrayToString -- CharArrayToString's own handling of invalid
// input is not something this file relies on or trusts; validity is
// established independently first, on the same raw bytes). A
// zero-length file decodes to "" with a true return (an empty file is
// not itself a failure).
bool ReadUtf8File(string path, string &outContent)
{
   outContent = "";
   uchar bytes[];
   if(!ReadRawBytes(path, bytes))
      return false;
   int len = ArraySize(bytes);
   if(len == 0)
      return true;
   if(!IsValidUtf8(bytes, len))
      return false;
   outContent = CharArrayToString(bytes, 0, len, UTF8_CODEPAGE);
   return true;
}

// Splits `content` on '\n', stripping a trailing '\r' from each line
// (defensive against CRLF, though this script only ever writes '\n')
// and dropping the single trailing empty element produced by a final
// newline.
int SplitUtf8Lines(string content, string &outLines[])
{
   int n = StringSplit(content, '\n', outLines);
   for(int i = 0; i < n; i++)
   {
      int lineLen = StringLen(outLines[i]);
      if(lineLen > 0 && StringGetCharacter(outLines[i], lineLen - 1) == '\r')
         outLines[i] = StringSubstr(outLines[i], 0, lineLen - 1);
   }
   if(n > 0 && StringLen(outLines[n - 1]) == 0)
   {
      n--;
      ArrayResize(outLines, n);
   }
   return n;
}

// Quote-aware CSV row split (mirrors Python's csv module and
// EconomicCalendarExporter.mq5's own EscapeCsv): a double-quoted field
// may contain commas and doubled ("") embedded quotes. Returns false
// unless the row splits into EXACTLY CSV_COLUMN_COUNT fields AND every
// opened quote was closed -- a row ending while still inside a quoted
// field (eighth review: an interrupted write stopped mid-field) is
// NEVER treated as complete, even if the field count happens to come
// out right by coincidence.
bool SplitCsvRow(string line, string &fields[])
{
   ArrayResize(fields, CSV_COLUMN_COUNT);
   int fieldIdx = 0;
   string current = "";
   bool inQuotes = false;
   int len = StringLen(line);
   int i = 0;
   while(i < len)
   {
      string ch = StringSubstr(line, i, 1);
      if(inQuotes)
      {
         if(ch == "\"")
         {
            if(i + 1 < len && StringSubstr(line, i + 1, 1) == "\"")
            {
               current += "\"";
               i += 2;
               continue;
            }
            inQuotes = false;
            i++;
            continue;
         }
         current += ch;
         i++;
         continue;
      }
      if(ch == "\"" && StringLen(current) == 0)
      {
         inQuotes = true;
         i++;
         continue;
      }
      if(ch == ",")
      {
         if(fieldIdx >= CSV_COLUMN_COUNT)
            return false;  // too many fields
         fields[fieldIdx] = current;
         fieldIdx++;
         current = "";
         i++;
         continue;
      }
      current += ch;
      i++;
   }
   if(inQuotes)
      return false;  // interrupted mid-quoted-field -- never a complete row
   if(fieldIdx >= CSV_COLUMN_COUNT)
      return false;
   fields[fieldIdx] = current;
   fieldIdx++;
   return (fieldIdx == CSV_COLUMN_COUNT);
}

//+------------------------------------------------------------------+
//| 64-bit FNV-1a hash over the UTF-8 byte representation of `s` --    |
//| NOT a cryptographic hash. See EconomicCalendarExporter.mq5's       |
//| "WINDOW INTEGRITY CONTRACT" comment for the full design rationale. |
//+------------------------------------------------------------------+
ulong Fnv1a64(string s)
{
   uchar bytes[];
   int n = StringToCharArray(s, bytes, 0, -1, UTF8_CODEPAGE);
   int len = (n > 0) ? n - 1 : 0;  // exclude the trailing NUL terminator
   ulong h = 0xcbf29ce484222325;
   for(int i = 0; i < len; i++)
      h = (h ^ (ulong)bytes[i]) * 0x100000001b3;
   return h;
}

// Canonical serialization of one row's RAW field text (already
// unescaped -- e.g. as returned by SplitCsvRow, or as originally
// fetched before CSV-escaping) -- fields[0] must be the row's
// value_id. Used identically on BOTH the "what got persisted"
// (RescanWindowDigest, re-parsing the CSV) and "what we intended to
// persist" (FetchMonthWindow, from the freshly fetched values) sides
// of the fetched-vs-persisted verification, so the two can never
// silently drift apart from each other.
string CanonicalRowString(const string &fields[])
{
   string canonical = fields[0];
   for(int i = 1; i < CSV_COLUMN_COUNT; i++)
      canonical += CharToString((uchar)0x1F) + fields[i];
   return canonical;
}

// Inserts/updates (id -> hash) in a small parallel-array map, keeping
// only the LAST occurrence for a repeated id (see WINDOW INTEGRITY
// CONTRACT: canonical records are deduplicated by value_id, keeping
// the latest occurrence -- handles overlapping re-fetches and genuine
// revisions identically on both sides of any comparison that uses
// this helper).
void UpsertCanonicalHash(string &ids[], ulong &hashes[], int &count, string id, ulong hash)
{
   for(int j = 0; j < count; j++)
   {
      if(ids[j] == id) { hashes[j] = hash; return; }
   }
   count++;
   ArrayResize(ids, count);
   ArrayResize(hashes, count);
   ids[count - 1] = id;
   hashes[count - 1] = hash;
}

//+------------------------------------------------------------------+
//| INTERRUPTED-WRITE TAIL RECOVERY (eighth review, P1)                |
//|                                                                    |
//| CONFIRMED FAILURE MECHANISM: this script's own writes are one      |
//| FileWriteArray call for a line's content bytes, then a SEPARATE     |
//| FileWriteArray call for its single trailing '\n' byte (see          |
//| WriteUtf8Line above). A process interruption between those two      |
//| calls -- or mid-way through the content bytes themselves, e.g.      |
//| inside a quoted field or inside a multibyte UTF-8 sequence -- can    |
//| leave the file's LAST line dangling: real content with no           |
//| terminating newline. The NEXT run seeks to FileSize() (current      |
//| end-of-file) and appends its own first line's bytes DIRECTLY onto    |
//| that fragment, with no separating newline -- producing one          |
//| garbled, unparseable combined line. RescanWindowDigest's own         |
//| per-row parse failure handling (`continue` on an unparseable row --  |
//| by design, so ONE bad legacy row can never fail an entire rescan)    |
//| then silently drops that merged line from the canonical record set  |
//| WITHOUT signalling anything is wrong -- and, without a SEPARATE      |
//| fetched-vs-persisted check (see FetchMonthWindow), a window whose    |
//| retry re-fetched N rows but only N-1 survived that merge could be    |
//| certified OK anyway, using a digest of only the rows that happen to  |
//| still parse.                                                        |
//|                                                                    |
//| This function establishes a VERIFIED safe append boundary for       |
//| `path` (used for BOTH OutputFile and the window-log file -- pass     |
//| `isCsvFormat=false` for the log, whose row shape is a plain          |
//| exactly-10-field comma split with no quoting) BEFORE anything is     |
//| ever appended to it:                                                 |
//|   - file absent, empty, or already ending in '\n' -> CLEAN, nothing  |
//|     changed.                                                         |
//|   - the last line is a COMPLETE, structurally valid record (right    |
//|     field count; no unterminated quote; valid UTF-8) that is         |
//|     simply missing its trailing newline -- this happens when the     |
//|     interruption landed exactly between WriteUtf8Bytes and its       |
//|     newline byte, with the content itself fully written -- the       |
//|     single missing '\n' byte is appended (itself checked), and       |
//|     nothing is ever dropped.                                         |
//|   - the last line is INCOMPLETE or malformed (truncated mid-quoted-  |
//|     field, mid-multibyte-UTF-8-sequence, or wrong field count) --     |
//|     the only safe boundary is immediately after the last confirmed   |
//|     complete line. The recovered PREFIX (everything up to and        |
//|     including that boundary) is written to a BRAND-NEW temp file,    |
//|     verified byte-for-byte by reading it back, and ONLY THEN          |
//|     atomically swapped in over the original via FileMove -- `path`   |
//|     is never opened in a truncating mode as part of this recovery,   |
//|     so ANY failure at ANY step (create temp, write, verify, or the    |
//|     final move) aborts with `path` left completely untouched.        |
//+------------------------------------------------------------------+
enum ENUM_TAIL_RECOVERY
{
   TAIL_RECOVERY_CLEAN,     // nothing to recover -- file already ends at a safe boundary
   TAIL_RECOVERY_REPAIRED,  // an interrupted trailing record was found and safely repaired
   TAIL_RECOVERY_ERROR      // could not safely repair -- caller MUST abort
};

// Determines whether bytes[tailStart..len) form one COMPLETE record of
// the given format. `isCsvFormat=true` uses the quote-aware
// CSV_COLUMN_COUNT split; `false` uses a plain comma split expecting
// exactly 10 fields (the window-log row shape). The tail's bytes must
// independently be valid UTF-8 -- a sequence truncated mid-character
// is immediate proof of incompleteness, checked before any decode is
// attempted.
bool IsCompleteTailRecord(const uchar &tailBytes[], int tailLen, bool isCsvFormat)
{
   if(!IsValidUtf8(tailBytes, tailLen))
      return false;
   string tailText = CharArrayToString(tailBytes, 0, tailLen, UTF8_CODEPAGE);
   if(isCsvFormat)
   {
      string fields[];
      return SplitCsvRow(tailText, fields);
   }
   string fields[];
   int fc = StringSplit(tailText, ',', fields);
   return (fc == 10);
}

// Pure boundary computation over an in-memory buffer (no file I/O) --
// kept separate from RecoverFileTail so the decision logic can be
// exercised directly against synthetic byte arrays in a self-test
// without needing a real file on disk for every case.
void ComputeSafeAppendBoundary(const uchar &bytes[], int len, bool isCsvFormat, long &outSafeOffset, bool &outLastLineComplete)
{
   int lastNl = -1;
   for(int i = len - 1; i >= 0; i--)
   {
      if(bytes[i] == (uchar)'\n') { lastNl = i; break; }
   }
   int tailStart = lastNl + 1;
   int tailLen = len - tailStart;

   if(tailLen == 0)
   {
      outSafeOffset = len;
      outLastLineComplete = true;  // trivially -- there IS no dangling tail
      return;
   }

   uchar tailBytes[];
   ArrayResize(tailBytes, tailLen);
   for(int i = 0; i < tailLen; i++)
      tailBytes[i] = bytes[tailStart + i];

   if(IsCompleteTailRecord(tailBytes, tailLen, isCsvFormat))
   {
      outSafeOffset = len;         // keep everything; only the newline is missing
      outLastLineComplete = false; // signal: caller must append '\n' before anything else
      return;
   }

   outSafeOffset = tailStart;      // drop the incomplete/malformed tail
   outLastLineComplete = true;     // the kept prefix already ends at '\n' (or is empty)
}

ENUM_TAIL_RECOVERY RecoverFileTail(string path, bool isCsvFormat)
{
   if(!FileIsExist(path))
      return TAIL_RECOVERY_CLEAN;  // nothing to recover for a file that doesn't exist yet

   uchar bytes[];
   if(!ReadRawBytes(path, bytes))
   {
      PrintFormat("[CalendarExporterCore] ERROR: could not read existing %s to check for an interrupted "
                  "trailing record -- ABORTING. %s is left completely untouched.", path, path);
      return TAIL_RECOVERY_ERROR;
   }

   int len = ArraySize(bytes);
   if(len == 0)
      return TAIL_RECOVERY_CLEAN;

   long safeOffset;
   bool lastLineComplete;
   ComputeSafeAppendBoundary(bytes, len, isCsvFormat, safeOffset, lastLineComplete);

   if(safeOffset == len && lastLineComplete)
      return TAIL_RECOVERY_CLEAN;  // already ends cleanly at a newline (or is empty)

   if(safeOffset == len && !lastLineComplete)
   {
      // Complete record, just missing its newline -- append exactly one
      // '\n' byte. Nothing is ever read back into memory or rewritten;
      // this is a pure, checked, single-byte append.
      int h = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
      if(h == INVALID_HANDLE)
      {
         PrintFormat("[CalendarExporterCore] ERROR: could not open %s to complete its trailing record's "
                     "missing newline -- ABORTING. %s is left completely untouched.", path, path);
         return TAIL_RECOVERY_ERROR;
      }
      if(!FileSeek(h, (long)len, SEEK_SET))
      {
         PrintFormat("[CalendarExporterCore] ERROR: could not seek to end of %s to complete its trailing "
                     "record's missing newline -- ABORTING. %s is left completely untouched.", path, path);
         FileClose(h);
         return TAIL_RECOVERY_ERROR;
      }
      uchar nl[1];
      nl[0] = (uchar)'\n';
      uint written = FileWriteArray(h, nl, 0, 1);
      if(written == 1)
         FileFlush(h);
      FileClose(h);
      if(written != 1)
      {
         PrintFormat("[CalendarExporterCore] ERROR: writing the completing newline to %s failed -- "
                     "ABORTING.", path);
         return TAIL_RECOVERY_ERROR;
      }
      PrintFormat("[CalendarExporterCore] RECOVERED %s: its last record was complete but missing a "
                  "terminating newline (an interruption landed between the record's content and its "
                  "newline byte) -- appended the single missing byte; no record was dropped.", path);
      return TAIL_RECOVERY_REPAIRED;
   }

   // Incomplete/malformed tail -- the safe boundary is short of the
   // current file length. Recover via a brand-new temp file, verified
   // byte-for-byte, then an atomic replace -- `path` itself is never
   // opened in a truncating mode.
   int droppedBytes = (int)(len - safeOffset);
   string tmpPath = path + ".recover_tmp";
   if(FileIsExist(tmpPath))
      FileDelete(tmpPath);  // stale leftover from a previously-aborted recovery attempt

   int th = FileOpen(tmpPath, FILE_WRITE | FILE_BIN);
   if(th == INVALID_HANDLE)
   {
      PrintFormat("[CalendarExporterCore] ERROR: could not create %s to recover %s's interrupted "
                  "trailing record -- ABORTING. %s is left completely untouched.", tmpPath, path, path);
      return TAIL_RECOVERY_ERROR;
   }
   uint written = (safeOffset > 0) ? FileWriteArray(th, bytes, 0, (int)safeOffset) : 0;
   FileFlush(th);
   FileClose(th);
   if((long)written != safeOffset)
   {
      PrintFormat("[CalendarExporterCore] ERROR: recovery write to %s was incomplete (%d of %d bytes) -- "
                  "ABORTING. %s is left completely untouched.", tmpPath, written, safeOffset, path);
      FileDelete(tmpPath);
      return TAIL_RECOVERY_ERROR;
   }

   uchar verifyBytes[];
   if(!ReadRawBytes(tmpPath, verifyBytes) || ArraySize(verifyBytes) != safeOffset)
   {
      PrintFormat("[CalendarExporterCore] ERROR: could not verify recovered %s -- ABORTING. %s is left "
                  "completely untouched.", tmpPath, path);
      FileDelete(tmpPath);
      return TAIL_RECOVERY_ERROR;
   }
   for(int i = 0; i < (int)safeOffset; i++)
   {
      if(verifyBytes[i] != bytes[i])
      {
         PrintFormat("[CalendarExporterCore] ERROR: recovered %s content mismatch at byte %d -- "
                     "ABORTING. %s is left completely untouched.", tmpPath, i, path);
         FileDelete(tmpPath);
         return TAIL_RECOVERY_ERROR;
      }
   }

   if(!FileMove(tmpPath, 0, path, FILE_REWRITE))
   {
      PrintFormat("[CalendarExporterCore] ERROR: could not replace %s with its recovered version -- "
                  "ABORTING. %s is left completely untouched (the verified recovery remains at %s for "
                  "manual inspection).", path, path, tmpPath);
      return TAIL_RECOVERY_ERROR;
   }

   PrintFormat("[CalendarExporterCore] RECOVERED %s: dropped %d byte(s) of an incomplete/malformed "
               "trailing record left by an interrupted write. All earlier valid records are preserved "
               "byte-for-byte.", path, droppedBytes);
   return TAIL_RECOVERY_REPAIRED;
}

#endif // CALENDAR_EXPORTER_CORE_MQH
//+------------------------------------------------------------------+
