//+------------------------------------------------------------------+
//|                                    EconomicCalendarExporter.mq5 |
//|                                                                    |
//| Exports the MQL5 Economic Calendar to CSV for a single, wide      |
//| date range. Runs as a script inside MetaTrader 5.                 |
//|                                                                    |
//| WHY THIS EXISTS: the MQL5 Calendar API (CalendarValueHistory) is  |
//| only reachable from code running inside the terminal -- there is  |
//| no HTTP/REST endpoint Python can call directly. This script is    |
//| therefore the one piece of the pipeline that is NOT automatable   |
//| from Python and must be run manually inside MetaTrader.           |
//|                                                                    |
//| Everything this script does internally (monthly chunking, retry   |
//| on empty/failed windows, progress logging, CSV writing) mirrors   |
//| what the Python fetchers do for HTTP providers, so the *user*     |
//| still only specifies one wide date range -- StartDate/EndDate --  |
//| never a month-by-month loop.                                      |
//|                                                                    |
//| VERIFICATION STATUS (eighth review): this file HAS been compiled    |
//| and run inside an actual MetaTrader 5 terminal (via MetaEditor's    |
//| /compile CLI and a scripted terminal /config startup -- see          |
//| mql5_exporter/DigestSelfTest.mq5, FileModeProbe.mq5,                 |
//| FileRecoverySelfTest.mq5, and TailRecoverySelfTest.mq5, none of      |
//| which are part of the shipped pipeline). What that CONFIRMED,        |
//| empirically, on that build:                                          |
//|   - It compiles with 0 errors after fixing a REAL bug the compiler  |
//|     caught: CALENDAR_UNIT_MOM/YOY/QOQ do not exist in the real      |
//|     ENUM_CALENDAR_EVENT_UNIT (see UnitToString) -- the enum members |
//|     used now are copied from the official documentation at         |
//|     https://www.mql5.com/en/docs/constants/structures/mqlcalendar, |
//|     not recalled from memory.                                       |
//|   - `StringToCharArray`/`CharArrayToString` with a #define'd        |
//|     UTF8_CODEPAGE (65001) round-trip ASCII, accented text, curly    |
//|     quotes/em dash, non-Latin (CJK) text, and a supplementary-plane |
//|     (surrogate-pair) emoji correctly, and the trailing NUL          |
//|     exclusion is correct -- DigestSelfTest.mq5's 8 fixed vectors    |
//|     all produced digests matching the independently-computed        |
//|     Python-side expected values exactly (see                        |
//|     tests/test_mql5_digest_encoding_contract.py).                   |
//|   - `FileOpen(FILE_READ|FILE_WRITE|FILE_BIN)` does NOT truncate an  |
//|     existing file, but leaves the position at 0 (NOT end-of-file)   |
//|     even when the file is non-empty -- an explicit FileSeek to      |
//|     FileSize() is REQUIRED before appending, confirmed via          |
//|     FileModeProbe.mq5's Q2/Q5 (this drove the LogWindowStatus/      |
//|     FetchMonthWindow append fixes below).                           |
//|   - bare `FileOpen(FILE_WRITE|...)` DOES truncate an existing file  |
//|     immediately on open (FileModeProbe.mq5's Q3) -- confirming the  |
//|     root cause of the original LogWindowStatus destructive-rewrite  |
//|     bug (defect A) and why it is never used for the log anymore.   |
//|   - `FileSeek` returns bool success and `FileSize`/`FileWriteArray`/|
//|     `FileReadArray` behave as this file assumes (Q4).               |
//|   - `IsValidUtf8` (in CalendarExporterCore.mqh) correctly accepts    |
//|     every valid boundary code point and rejects overlong/surrogate/ |
//|     out-of-range/truncated/stray-continuation sequences -- 35/35    |
//|     FileRecoverySelfTest.mq5 vectors passed (eighth review, P2).    |
//|   - `RecoverFileTail`/`ComputeSafeAppendBoundary`/`IsValidUtf8Ex`     |
//|     (in CalendarExporterCore.mqh) correctly repair an interrupted    |
//|     trailing CSV/checkpoint record (dropped if incomplete, newline-  |
//|     completed if merely unterminated) without ever disturbing        |
//|     earlier valid records -- 34/34 TailRecoverySelfTest.mq5          |
//|     scenarios passed (eighth review, P1).                            |
//|   - `FileMove`'s signature (`bool FileMove(string src, int          |
//|     common_flag, string dst, int mode_flags)`, `FILE_REWRITE` to     |
//|     overwrite an existing destination) is confirmed against          |
//|     official documentation (mql5.com/en/docs/files/filemove), not    |
//|     recalled from memory -- though its call sites are only compiled, |
//|     never exercised by a self-test.                                  |
//| NOT yet verified: `CalendarValueHistory`/`CalendarEventById`/        |
//| `CalendarCountryById` themselves (no live calendar data has been     |
//| fetched in this environment) and `ulong` overflow wraparound         |
//| (standard documented C/C++ semantics MQL5 is documented to follow,  |
//| not independently re-derived). File ownership: every read/write in   |
//| this file is a SELF-CONTAINED open-operate-close sequence -- no      |
//| function returns while holding a handle open on a path another       |
//| function might also touch, EXCEPT the top-level writer handle in     |
//| OnStart(), which FetchMonthWindow explicitly closes before any read  |
//| of the same path (RescanWindowDigest) and reopens immediately after, |
//| checking every open/seek result (see FetchMonthWindow and the        |
//| "FILE HANDLE OWNERSHIP" note below).                                 |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

// Shared, script-input-independent helpers (UTF-8 file I/O, strict
// UTF-8 validation, quote-aware CSV parsing, the FNV-1a canonical
// digest, and interrupted-write tail recovery) -- also #include'd,
// UNMODIFIED, by this directory's self-test scripts, so a passing
// self-test proves something about the ACTUAL production code, not a
// separately hand-typed duplicate of it (eighth review). Defines
// UTF8_CODEPAGE, CSV_HEADER, and CSV_COLUMN_COUNT.
#include "CalendarExporterCore.mqh"

// DATE CONTRACT: both StartDate and EndDate are INCLUSIVE calendar
// dates -- "give me data through this day" -- matching the convention
// used everywhere on the Python side (src/data/dates.py DateChunk.end
// is always inclusive). EndDate is converted, ONCE, to an exclusive
// midnight boundary (the day after) in OnStart() before the monthly
// chunking loop runs; nothing below this point ever special-cases
// whether a window happens to land on a natural month boundary.
input datetime StartDate     = D'2016.01.01 00:00:00';
input datetime EndDate       = D'2026.09.10 00:00:00';  // inclusive -- data through this day
input string   CountryCode   = "US";
input string   CurrencyCode  = "USD";
input string   OutputFile    = "us_macro_calendar.csv"; // written under MQL5\Files\

// Bump this whenever the CSV column layout, the window-log identity, OR
// the file-encoding/digest algorithm changes. Used both for explicit
// legacy-CSV migration and to derive a versioned window-log filename,
// so a schema/encoding change can never cause new code to silently
// misinterpret an old-format log/CSV.
//   v3 -> v4: window log row gained a per-window CONTENT DIGEST.
//   v4 -> v5 (sixth review): CSV/log text and the digest hash are now
//     explicitly UTF-8 on both this side and the Python side -- v4 (and
//     earlier) evidence may have been written as ANSI (system codepage)
//     bytes and/or hashed as UTF-16 code units, which is NOT guaranteed
//     to equal UTF-8 bytes for any non-ASCII content. v5 evidence is
//     never generated from, or compared against, v4 (or earlier) files.
#define EXPORT_SCHEMA_VERSION "5"

// CSV_HEADER, CSV_COLUMN_COUNT, UTF8_CODEPAGE are defined in
// CalendarExporterCore.mqh (included above).

string WindowLogFile = "";  // derived from OutputFile + EXPORT_SCHEMA_VERSION in OnStart()

//+------------------------------------------------------------------+
//| WINDOW INTEGRITY CONTRACT (fifth/sixth review)                     |
//|                                                                    |
//| A window-log "OK" entry is trusted only when the CSV's CURRENT     |
//| content for that exact window still matches a per-window CONTENT   |
//| DIGEST recorded at successful export time -- not a plain row       |
//| count, not a file creation date, not a whole-file checksum.        |
//|                                                                    |
//| Design decisions (mirrored exactly on the Python side --           |
//| src/data/fetch/mql5.py's module docstring):                        |
//|   - Digests/counts apply to CANONICAL records, not physical        |
//|     exported rows: deduplicated by value_id, keeping the LAST      |
//|     occurrence in file order (handles overlapping re-fetches and   |
//|     genuine revisions).                                            |
//|   - The digest is a small, from-scratch 64-bit FNV-1a hash over    |
//|     each canonical row's fields joined by the ASCII unit separator |
//|     (0x1F), computed over the row's REAL UTF-8 BYTE representation |
//|     -- deliberately NOT a cryptographic hash, and NOT reliant on   |
//|     any MQL5 crypto-library call. Per-row hashes are XOR-combined  |
//|     (order-independent).                                           |
//|   - FILE ENCODING CONTRACT: every CSV/log file this script reads   |
//|     or writes is UTF-8 text, always -- never FILE_ANSI (system     |
//|     codepage) or FILE_UNICODE (UTF-16LE) text mode, neither of     |
//|     which can produce UTF-8 bytes. All I/O goes through FILE_BIN   |
//|     plus explicit StringToCharArray/CharArrayToString(..., UTF8_   |
//|     CODEPAGE) conversion (see WriteUtf8Bytes/WriteUtf8Line/         |
//|     ReadUtf8File below), with the auto-appended NUL terminator      |
//|     from StringToCharArray always excluded.                        |
//|   - Evidence describes the data AT SUCCESSFUL EXPORT TIME.         |
//|     WindowAlreadyOk only ever COMPARES a freshly recomputed digest  |
//|     against the log's recorded one -- it never rewrites the log,   |
//|     so damaged data can never be "blessed" into new evidence.      |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
// MQL5 calendar values are integers scaled by a FIXED 1,000,000
// factor, independent of the event's display "digits". Do NOT use
// MathPow(10, ev.digits) -- digits only controls how many decimals to
// SHOW, not how the raw long is scaled. See:
// https://www.mql5.com/en/docs/constants/structures/mqlcalendar
#define MQL5_CALENDAR_VALUE_SCALE 1000000.0

//+------------------------------------------------------------------+
datetime MonthStart(datetime t)
{
   MqlDateTime s;
   TimeToStruct(t, s);
   s.day = 1;
   s.hour = 0; s.min = 0; s.sec = 0;
   return StructToTime(s);
}

datetime NextMonthStart(datetime monthStart)
{
   MqlDateTime s;
   TimeToStruct(monthStart, s);
   s.mon += 1;
   if(s.mon > 12) { s.mon = 1; s.year += 1; }
   s.day = 1; s.hour = 0; s.min = 0; s.sec = 0;
   return StructToTime(s);
}

// Zero the time-of-day component, keeping the calendar date.
datetime DayStart(datetime t)
{
   MqlDateTime s;
   TimeToStruct(t, s);
   s.hour = 0; s.min = 0; s.sec = 0;
   return StructToTime(s);
}

// Midnight of the calendar day immediately after `dayStart`. MQL5
// `datetime` values are not DST-adjusted, so a flat +86400 seconds is
// exactly one calendar day here -- no ambiguity around DST transitions.
datetime NextDayStart(datetime dayStart)
{
   return dayStart + 86400;
}

string EscapeCsv(string value)
{
   // Minimal CSV escaping: wrap in quotes if it contains a comma/quote,
   // and double any embedded quotes -- matches the quoting convention
   // Python's csv module (and SplitCsvRow below) expect.
   if(StringFind(value, ",") >= 0 || StringFind(value, "\"") >= 0)
   {
      StringReplace(value, "\"", "\"\"");
      value = "\"" + value + "\"";
   }
   return value;
}

string ImportanceToString(ENUM_CALENDAR_EVENT_IMPORTANCE imp)
{
   switch(imp)
   {
      case CALENDAR_IMPORTANCE_HIGH:   return "HIGH";
      case CALENDAR_IMPORTANCE_MODERATE: return "MEDIUM";
      case CALENDAR_IMPORTANCE_LOW:    return "LOW";
      default:                         return "NONE";
   }
}

// ev.unit (ENUM_CALENDAR_EVENT_UNIT) and ev.multiplier
// (ENUM_CALENDAR_EVENT_MULTIPLIER) are TWO DIFFERENT enum types --
// kept as two distinct functions/types here so they can never be
// conflated.
//
// COMPILER-VERIFIED (seventh review): a prior version of this switch
// referenced CALENDAR_UNIT_MOM/CALENDAR_UNIT_YOY/CALENDAR_UNIT_QOQ --
// these do NOT exist in the real ENUM_CALENDAR_EVENT_UNIT and failed to
// compile ("undeclared identifier") the first time this file was
// actually compiled in MetaEditor. The real enum (confirmed against
// https://www.mql5.com/en/docs/constants/structures/mqlcalendar) also
// has SIX members this file previously had no case for at all (they
// fell through to "UNKNOWN", which is still the correct, conservative
// behavior -- see normalize/mql5.py's `_PERCENT_UNITS` comment on why
// only PERCENT is treated as a known, comparable unit).
string UnitToString(ENUM_CALENDAR_EVENT_UNIT u)
{
   switch(u)
   {
      case CALENDAR_UNIT_PERCENT:    return "PERCENT";
      case CALENDAR_UNIT_CURRENCY:   return "CURRENCY";
      case CALENDAR_UNIT_HOUR:       return "HOUR";
      case CALENDAR_UNIT_JOB:        return "JOB";
      case CALENDAR_UNIT_RIG:        return "RIG";
      case CALENDAR_UNIT_USD:        return "USD";
      case CALENDAR_UNIT_PEOPLE:     return "PEOPLE";
      case CALENDAR_UNIT_MORTGAGE:   return "MORTGAGE";
      case CALENDAR_UNIT_VOTE:       return "VOTE";
      case CALENDAR_UNIT_BARREL:     return "BARREL";
      case CALENDAR_UNIT_CUBICFEET:  return "CUBICFEET";
      case CALENDAR_UNIT_POSITION:   return "POSITION";
      case CALENDAR_UNIT_BUILDING:   return "BUILDING";
      default:                       return "UNKNOWN";
   }
}

string MultiplierToString(ENUM_CALENDAR_EVENT_MULTIPLIER m)
{
   switch(m)
   {
      case CALENDAR_MULTIPLIER_THOUSANDS: return "THOUSANDS";
      case CALENDAR_MULTIPLIER_MILLIONS:  return "MILLIONS";
      case CALENDAR_MULTIPLIER_BILLIONS:  return "BILLIONS";
      case CALENDAR_MULTIPLIER_TRILLIONS: return "TRILLIONS";
      case CALENDAR_MULTIPLIER_NONE:      return "NONE";
      default:                            return "UNKNOWN";
   }
}

string DoubleOrEmpty(double v, bool isSet)
{
   if(!isSet) return "";
   return DoubleToString(v, 6);
}

// UTF-8 binary file I/O (WriteUtf8Bytes, WriteUtf8Line, ReadRawBytes,
// ReadUtf8File), strict UTF-8 validation (IsValidUtf8), line/row
// splitting (SplitUtf8Lines, SplitCsvRow), the FNV-1a canonical digest
// (Fnv1a64, CanonicalRowString, UpsertCanonicalHash), and interrupted-
// write tail recovery (RecoverFileTail) all now live in
// CalendarExporterCore.mqh (included above) -- see that file for the
// full design rationale on each.

//+------------------------------------------------------------------+
//| Re-reads OutputFile as UTF-8 text for every row whose event_time   |
//| falls within [winStart, winEndExclusive), builds the CANONICAL     |
//| (deduplicated-by-value_id, last-occurrence-wins) record set, and   |
//| returns its (count, digest) AND the underlying (outIds, outHashes) |
//| parallel arrays -- exposed so FetchMonthWindow can cross-check      |
//| that every record it just fetched actually made it into this        |
//| PERSISTED set with the correct content (eighth review, P1's         |
//| fetched-vs-persisted verification), not merely that the digest is   |
//| internally self-consistent with whatever happens to still parse.    |
//|                                                                    |
//| FILE HANDLE OWNERSHIP: this function performs one SELF-CONTAINED   |
//| open-read-close of OutputFile (via ReadUtf8File) and never leaves  |
//| a handle open. It is still the CALLER's responsibility not to hold |
//| a WRITE handle open on OutputFile when calling this -- FetchMonth- |
//| Window explicitly closes its write handle first (see below); a     |
//| resume-time WindowAlreadyOk check is made BEFORE any writer is      |
//| ever opened (see OnStart), so no conflict is possible there either.|
//+------------------------------------------------------------------+
bool RescanWindowDigest(datetime winStart, datetime winEndExclusive, int &outCount, string &outDigest, string &outIds[], ulong &outHashes[])
{
   outCount = 0;
   outDigest = "";
   ArrayResize(outIds, 0);
   ArrayResize(outHashes, 0);

   string content;
   if(!ReadUtf8File(OutputFile, content))
      return false;

   string lines[];
   int lineCount = SplitUtf8Lines(content, lines);
   if(lineCount == 0)
      return false;  // not even a header -- cannot verify anything

   int n = 0;
   for(int li = 1; li < lineCount; li++)  // skip the header row (index 0)
   {
      if(StringLen(lines[li]) == 0)
         continue;

      string fields[];
      if(!SplitCsvRow(lines[li], fields))
         continue;  // malformed/truncated row -- not counted, never fails the whole rescan

      datetime eventTime = StringToTime(fields[6]);  // event_time is column index 6
      if(eventTime < winStart || eventTime >= winEndExclusive)
         continue;

      ulong rowHash = Fnv1a64(CanonicalRowString(fields));
      UpsertCanonicalHash(outIds, outHashes, n, fields[0], rowHash);
   }

   ulong combined = 0;
   for(int i = 0; i < n; i++)
      combined ^= outHashes[i];

   outCount = n;
   outDigest = StringFormat("%016I64x", combined);
   return true;
}

//+------------------------------------------------------------------+
//| Legacy CSV migration: never append the current schema underneath   |
//| a header written by an older/different version of this script.    |
//|                                                                    |
//| Returns an explicit three-way outcome so "compatible header,       |
//| append in place" and "failed to read/move an incompatible file"    |
//| can never be confused by the caller (a bare bool previously        |
//| collapsed both into `false`, letting OnStart() fall through into   |
//| APPEND MODE on a still-incompatible file after a failed migration).|
//+------------------------------------------------------------------+
enum ENUM_MIGRATION_OUTCOME
{
   MIGRATION_READY_EXISTING,  // compatible header already present -- append in place
   MIGRATION_READY_NEW,       // no file existed, or an incompatible file was moved aside -- start fresh
   MIGRATION_ERROR            // could not safely proceed -- caller MUST abort, nothing else may run
};

ENUM_MIGRATION_OUTCOME MigrateLegacyCsvIfNeeded()
{
   if(!FileIsExist(OutputFile))
      return MIGRATION_READY_NEW;

   uchar bytes[];
   if(!ReadRawBytes(OutputFile, bytes))
   {
      PrintFormat("[MQL5 Exporter] ERROR: could not read existing %s to check its header/encoding "
                  "-- ABORTING. No calendar data will be requested and nothing will be appended.",
                  OutputFile);
      return MIGRATION_ERROR;
   }

   int len = ArraySize(bytes);
   // A v4-or-earlier ANSI export's header TEXT is pure ASCII, which is
   // byte-identical whether the file was written as FILE_ANSI or UTF-8
   // -- comparing decoded header strings alone can never tell them
   // apart. Validate the RAW BYTES against RFC 3629 first; only a file
   // that is genuinely valid UTF-8 is ever eligible for
   // MIGRATION_READY_EXISTING, regardless of what its header decodes to.
   //
   // TRUNCATION-TOLERANT (eighth review, P1): an interrupted write can
   // leave the file's LAST byte sequence cut short mid-multibyte-
   // character -- a plain whole-buffer IsValidUtf8 check would then
   // reject the ENTIRE file (including every earlier, perfectly valid
   // record) as "incompatible" and migrate the whole history aside
   // needlessly. IsValidUtf8Ex distinguishes that specific case
   // (`outTruncatedAtEnd`, with `outValidPrefixLen` bytes confirmed
   // good before it) from every OTHER kind of invalidity (bad lead/
   // continuation byte, overlong encoding, surrogate, out-of-range --
   // still treated as a genuinely incompatible file, e.g. real ANSI
   // content). Only the confirmed-valid PREFIX is ever decoded here;
   // RecoverFileTail (called right after this function returns, from
   // OnStart) repairs the truncated tail itself afterward.
   bool truncatedAtEnd = false;
   int validPrefixLen = len;
   bool wholeBufferValid = (len == 0) || IsValidUtf8Ex(bytes, len, truncatedAtEnd, validPrefixLen);
   bool safeToTrustPrefix = wholeBufferValid || truncatedAtEnd;
   int decodeLen = wholeBufferValid ? len : validPrefixLen;

   string existingHeader = "";
   if(safeToTrustPrefix && decodeLen > 0)
   {
      string content = CharArrayToString(bytes, 0, decodeLen, UTF8_CODEPAGE);
      // Strip a leading UTF-8 BOM (U+FEFF), if present -- this script
      // never writes one, but a manually-edited v5 file might have one
      // added by another tool; it must not itself defeat the header match.
      if(StringLen(content) > 0 && StringGetCharacter(content, 0) == 0xFEFF)
         content = StringSubstr(content, 1);
      int newlineIdx = StringFind(content, "\n");
      existingHeader = (newlineIdx >= 0) ? StringSubstr(content, 0, newlineIdx) : content;
      int headerLen = StringLen(existingHeader);
      if(headerLen > 0 && StringGetCharacter(existingHeader, headerLen - 1) == '\r')
         existingHeader = StringSubstr(existingHeader, 0, headerLen - 1);
   }

   if(safeToTrustPrefix && existingHeader == CSV_HEADER)
      return MIGRATION_READY_EXISTING; // confirmed UTF-8 (modulo a tolerated trailing truncation) AND exact current schema

   // Either the file is not valid UTF-8 for a reason OTHER than a
   // tolerated trailing truncation (almost certainly a v4-or-earlier
   // ANSI-encoded export, or genuine corruption) or its header text
   // doesn't match -- in both cases, preserve it untouched and require
   // a fresh export. We deliberately never attempt to guess the old
   // system codepage and re-encode it: the codepage used to write a v4
   // file is a machine/locale setting that is not recorded anywhere in
   // the file itself, so any specific codepage choice here would be a
   // guess, and a wrong guess would silently corrupt historical text.
   string reasonMsg = !safeToTrustPrefix
      ? "its bytes are not valid UTF-8 (most likely a pre-v5 ANSI-encoded export, or corruption)"
      : StringFormat("its header (found %s, expected %s) does not match", existingHeader, CSV_HEADER);

   string timestamp = TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS);
   StringReplace(timestamp, ".", "");
   StringReplace(timestamp, ":", "");
   StringReplace(timestamp, " ", "_");
   string legacyName = OutputFile + ".legacy_" + timestamp + ".csv";

   if(!FileMove(OutputFile, 0, legacyName, 0))
   {
      PrintFormat("[MQL5 Exporter] ERROR: %s is incompatible with the current UTF-8 schema (%s) but "
                  "could not move it aside to %s. ABORTING -- the original file is left completely "
                  "untouched; no calendar data will be requested and nothing will be appended to it "
                  "this run.",
                  OutputFile, reasonMsg, legacyName);
      return MIGRATION_ERROR;
   }
   PrintFormat("[MQL5 Exporter] Existing %s is incompatible with the current UTF-8 schema (%s) -- "
               "moved aside to %s. Starting a fresh UTF-8 export; this script never guesses the "
               "legacy file's original codepage, so its historical rows are NOT converted "
               "automatically -- convert them separately with a known source encoding (or simply "
               "re-export the full range fresh) if you need that history back.",
               OutputFile, reasonMsg, legacyName);
   return MIGRATION_READY_NEW;
}

//+------------------------------------------------------------------+
//| Durable per-window status log, keyed by EXACT window boundaries +  |
//| country/currency + schema version -- NOT just (year, month).       |
//| Self-contained open-write-close: never leaves the log file open.  |
//|                                                                    |
//| DURABLE APPEND, NOT READ-TRUNCATE-REWRITE (seventh review, defect  |
//| A): a prior version read the entire existing log into memory, then |
//| opened it with bare FILE_WRITE (which TRUNCATES on open --         |
//| empirically confirmed on this build via                            |
//| mql5_exporter/FileModeProbe.mq5, not part of the shipped pipeline) |
//| BEFORE writing the old content back plus the new line. Any         |
//| interruption between that truncating open and the rewrite -- or     |
//| any failure in the earlier read that this function's caller never  |
//| checked -- destroyed every prior checkpoint record, not just the   |
//| one being added. Also empirically confirmed: FILE_READ|FILE_WRITE  |
//| does NOT truncate on open, but leaves the position at 0 (not EOF)  |
//| even on a non-empty file -- an explicit FileSeek to FileSize() is  |
//| REQUIRED before writing, and its result is checked; on ANY failure |
//| (open, seek, or write) this function closes/discards the handle    |
//| and returns false WITHOUT ever using a truncating open, so an      |
//| existing log is never at risk of being erased by a failed append.  |
//+------------------------------------------------------------------+
bool LogWindowStatus(datetime winStart, datetime winEnd, string status, int canonicalRowCount, int attempts, string digest)
{
   int h = FileOpen(WindowLogFile, FILE_READ | FILE_WRITE | FILE_BIN);
   if(h == INVALID_HANDLE)
   {
      PrintFormat("[MQL5 Exporter] WARNING: could not open window log %s for durable append (error %d)",
                  WindowLogFile, GetLastError());
      return false;
   }

   ulong endPos = FileSize(h);
   if(!FileSeek(h, (long)endPos, SEEK_SET))
   {
      PrintFormat("[MQL5 Exporter] WARNING: could not seek to end of window log %s (error %d) -- "
                  "NOT writing (an unseeked write could overwrite existing checkpoint history)",
                  WindowLogFile, GetLastError());
      FileClose(h);
      return false;
   }

   string line = StringFormat(
      "%s,%s,%s,%s,%s,%s,%d,%d,%s,%s",
      TimeToString(winStart, TIME_DATE | TIME_SECONDS),
      TimeToString(winEnd, TIME_DATE | TIME_SECONDS),
      CountryCode,
      CurrencyCode,
      EXPORT_SCHEMA_VERSION,
      status,
      canonicalRowCount,
      attempts,
      TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS),
      digest
   );
   bool ok = WriteUtf8Line(h, line);
   if(ok)
      FileFlush(h);
   FileClose(h);

   if(!ok)
      PrintFormat("[MQL5 Exporter] WARNING: failed writing window log entry to %s -- prior history "
                  "was never truncated and remains intact", WindowLogFile);
   return ok;
}

//+------------------------------------------------------------------+
//| True only if a previous run logged this EXACT window (same start,  |
//| end, country, currency, schema version) as OK AND the CSV's        |
//| CURRENT content for this window still matches the digest recorded  |
//| at that time. The LATEST matching log row wins. MUST be called     |
//| with no write handle open on OutputFile or WindowLogFile -- see    |
//| OnStart(), which calls this BEFORE ever opening a writer.          |
//+------------------------------------------------------------------+
bool WindowAlreadyOk(datetime winStart, datetime winEndExclusive)
{
   if(!FileIsExist(WindowLogFile) || !FileIsExist(OutputFile))
      return false;

   string content;
   if(!ReadUtf8File(WindowLogFile, content))
      return false;

   string lines[];
   int lineCount = SplitUtf8Lines(content, lines);

   string wantStart = TimeToString(winStart, TIME_DATE | TIME_SECONDS);
   string wantEnd = TimeToString(winEndExclusive, TIME_DATE | TIME_SECONDS);

   bool found = false;
   string latestStatus = "";
   int latestCount = -1;
   string latestDigest = "";

   for(int li = 0; li < lineCount; li++)
   {
      if(StringLen(lines[li]) == 0)
         continue;

      // Window-log fields never contain commas/quotes (all generated
      // by this script -- dates, codes, digest hex) -- a plain split
      // is sufficient and avoids the quote-aware CSV parser used for
      // calendar-event text.
      string parts[];
      int fieldCount = StringSplit(lines[li], ',', parts);
      if(fieldCount != 10)
         continue;  // malformed/incomplete row -- never certifies completion

      string logStart = parts[0];
      string logEnd = parts[1];
      string logCountry = parts[2];
      string logCurrency = parts[3];
      string logSchema = parts[4];
      string status = parts[5];
      string rowCountStr = parts[6];
      string digest = parts[9];

      if(logStart == wantStart && logEnd == wantEnd &&
         logCountry == CountryCode && logCurrency == CurrencyCode &&
         logSchema == EXPORT_SCHEMA_VERSION)
      {
         // Overwrite with the LATEST matching row -- a subsequent
         // FAILED entry for this exact window must supersede an
         // earlier OK, not merely be ignored.
         found = true;
         latestStatus = status;
         latestCount = (int)StringToInteger(rowCountStr);
         latestDigest = digest;
      }
   }

   if(!found || latestStatus != "OK" || latestDigest == "")
      return false; // no evidence, or malformed/non-OK evidence -- never certifies completion

   int currentCount;
   string currentDigest;
   string discardIds[];
   ulong discardHashes[];
   if(!RescanWindowDigest(winStart, winEndExclusive, currentCount, currentDigest, discardIds, discardHashes))
      return false;

   return (currentCount == latestCount && currentDigest == latestDigest);
}

//+------------------------------------------------------------------+
//| Fetch one calendar-month window, with retry on empty/failed calls |
//| and integrity-verified success logging.                           |
//|                                                                    |
//| `fileHandle` is passed BY REFERENCE and is CLOSED, then REOPENED   |
//| in append mode, around the RescanWindowDigest call below -- see    |
//| "FILE HANDLE OWNERSHIP" on RescanWindowDigest. Every open/close/    |
//| reopen/seek here is checked explicitly; on ANY failure this        |
//| function logs FAILED (never OK) and returns without having         |
//| published successful checkpoint evidence.                          |
//+------------------------------------------------------------------+
int FetchMonthWindow(datetime winStart, datetime winEnd, int &fileHandle, int &totalRows, string &failedWindows)
{
   MqlCalendarValue values[];
   int attempts = 0;
   int maxAttempts = 3;
   int got = -1;

   while(attempts < maxAttempts)
   {
      attempts++;
      got = CalendarValueHistory(values, winStart, winEnd, CountryCode, CurrencyCode);
      if(got >= 0)
         break;
      Print("  retry ", attempts, "/", maxAttempts, " for window ",
            TimeToString(winStart, TIME_DATE), " -> ", TimeToString(winEnd, TIME_DATE),
            " (error ", GetLastError(), ")");
      Sleep(1000 * attempts);
   }

   if(got < 0)
   {
      failedWindows += TimeToString(winStart, TIME_DATE) + " to " + TimeToString(winEnd, TIME_DATE) + "; ";
      PrintFormat("[MQL5 Exporter] FAILED window %s -> %s after %d attempts",
                  TimeToString(winStart, TIME_DATE), TimeToString(winEnd, TIME_DATE), maxAttempts);
      LogWindowStatus(winStart, winEnd, "FAILED", 0, attempts, "");
      return 0;
   }

   int rows = ArraySize(values);
   bool writeOk = true;
   // Tracks, canonically (last-occurrence-wins by value_id -- see
   // CanonicalRowString/UpsertCanonicalHash), every record this run
   // INTENDED to persist for this window. Compared against
   // RescanWindowDigest's PERSISTED canonical set below (eighth
   // review, P1) so a window can never be certified OK merely because
   // *some* digest of whatever still parses happens to be internally
   // self-consistent -- it must contain everything this run actually
   // fetched, unaltered.
   string expectedIds[];
   ulong expectedHashes[];
   int expectedCount = 0;

   for(int i = 0; i < rows && writeOk; i++)
   {
      MqlCalendarEvent ev;
      MqlCalendarCountry ctry;
      string eventName = "";
      ENUM_CALENDAR_EVENT_UNIT eventUnit = CALENDAR_UNIT_NONE;
      ENUM_CALENDAR_EVENT_MULTIPLIER eventMultiplier = CALENDAR_MULTIPLIER_NONE;
      if(CalendarEventById(values[i].event_id, ev))
      {
         eventName = ev.name;
         eventUnit = ev.unit;
         eventMultiplier = ev.multiplier;
      }
      string countryCodeOut = CountryCode;
      if(CalendarCountryById(ev.country_id, ctry))
         countryCodeOut = ctry.code;

      // NOTE ON TIMEZONE: values[i].time comes from the terminal/broker
      // environment. We do NOT assert this is UTC -- record it as-is and
      // let the Python side treat it as "SERVER" origin unless the
      // operator has explicitly configured the broker's real timezone
      // in config/data_sources.yaml (providers.mql5.broker_timezone).
      //
      // values[i].period is the calendar PERIOD the value describes,
      // distinct from values[i].time (when it was/will be released) --
      // preserved for reference-period alignment against FRED's
      // observation dates (see src/data/reference_period.py).
      //
      // Fields are built as individual (unescaped) strings, in EXACT
      // CSV_HEADER column order, and used for TWO purposes: the CSV
      // line actually written (with EscapeCsv applied only where the
      // original text might need quoting) and the canonical hash of
      // what we EXPECT a correct rescan to find -- so the two can never
      // silently drift apart from each other (see CanonicalRowString).
      string rowFields[CSV_COLUMN_COUNT];
      rowFields[0]  = StringFormat("%I64u", values[i].id);
      rowFields[1]  = StringFormat("%I64u", values[i].event_id);
      rowFields[2]  = eventName;
      rowFields[3]  = countryCodeOut;
      rowFields[4]  = CurrencyCode;
      rowFields[5]  = ImportanceToString(ev.importance);
      rowFields[6]  = TimeToString(values[i].time, TIME_DATE | TIME_SECONDS);
      rowFields[7]  = TimeToString(values[i].period, TIME_DATE);
      rowFields[8]  = UnitToString(eventUnit);
      rowFields[9]  = MultiplierToString(eventMultiplier);
      rowFields[10] = DoubleOrEmpty(values[i].actual_value / MQL5_CALENDAR_VALUE_SCALE, values[i].actual_value != LONG_MIN);
      rowFields[11] = DoubleOrEmpty(values[i].forecast_value / MQL5_CALENDAR_VALUE_SCALE, values[i].forecast_value != LONG_MIN);
      rowFields[12] = DoubleOrEmpty(values[i].prev_value / MQL5_CALENDAR_VALUE_SCALE, values[i].prev_value != LONG_MIN);
      rowFields[13] = DoubleOrEmpty(values[i].revised_prev_value / MQL5_CALENDAR_VALUE_SCALE, values[i].revised_prev_value != LONG_MIN);
      rowFields[14] = StringFormat("%I64d", values[i].revision);
      rowFields[15] = "SERVER";

      string line = rowFields[0];
      for(int c = 1; c < CSV_COLUMN_COUNT; c++)
         line += "," + (c == 2 ? EscapeCsv(rowFields[c]) : rowFields[c]);

      UpsertCanonicalHash(expectedIds, expectedHashes, expectedCount, rowFields[0], Fnv1a64(CanonicalRowString(rowFields)));

      writeOk = WriteUtf8Line(fileHandle, line);
   }

   if(!writeOk)
   {
      // Never leave the handle in a state a subsequent window in THIS
      // run could write through (eighth review, P1): a row write can
      // fail partway -- e.g. its content bytes succeeded but the
      // newline byte did not -- leaving a dangling, non-newline-
      // terminated fragment at the current file position. Closing and
      // invalidating fileHandle here means the caller's loop (which
      // only checks `fileHandle == INVALID_HANDLE`) will skip every
      // remaining window this run rather than risk concatenating onto
      // that fragment; the NEXT run's upfront RecoverFileTail call
      // (see OnStart) safely repairs it before anything else happens.
      FileClose(fileHandle);
      fileHandle = INVALID_HANDLE;
      PrintFormat("[MQL5 Exporter] FAILED window %s -> %s: a row write to %s failed "
                  "-- NOT logging success",
                  TimeToString(winStart, TIME_DATE), TimeToString(winEnd, TIME_DATE), OutputFile);
      failedWindows += TimeToString(winStart, TIME_DATE) + " to " + TimeToString(winEnd, TIME_DATE) + " (write error); ";
      LogWindowStatus(winStart, winEnd, "FAILED", 0, attempts, "");
      return 0;
   }

   // FileFlush has no documented return value to check -- treated here
   // purely as "ensure the OS buffer is pushed" before the handle is
   // closed just below (FileClose itself flushes too, per documented
   // behavior, but the explicit call keeps intent visible).
   FileFlush(fileHandle);

   // Close the write handle before rescanning -- RescanWindowDigest
   // performs its own independent open of OutputFile, and this file
   // never relies on two concurrently open handles to the same path
   // (see the top-of-file UNVERIFIED note). Reopen in append mode
   // afterward regardless of outcome, so the caller's loop and final
   // FileClose still see a valid handle; every step here is checked.
   FileClose(fileHandle);
   fileHandle = INVALID_HANDLE;

   int canonicalCount;
   string digest;
   string persistedIds[];
   ulong persistedHashes[];
   bool rescanOk = RescanWindowDigest(winStart, winEnd, canonicalCount, digest, persistedIds, persistedHashes);

   // FETCHED-VS-PERSISTED VERIFICATION (eighth review, P1): RescanWindow-
   // Digest's own digest is only ever INTERNALLY self-consistent -- it
   // describes whatever canonical rows currently parse, which is
   // exactly what let the confirmed defect through (an interrupted
   // write's retry could merge onto a dangling fragment, garbling ONE
   // record into an unparseable line that RescanWindowDigest silently
   // drops, while everything else still produced a perfectly self-
   // consistent digest for the REDUCED set). Cross-check every record
   // this run actually intended to persist (expectedIds/expectedHashes,
   // built above from the fetched `values[]`) against what the rescan
   // found: any fetched id that is missing, or present with a DIFFERENT
   // hash (altered/corrupted), fails verification outright.
   bool fetchVerified = rescanOk;
   string verifyDetail = "";
   if(rescanOk)
   {
      int persistedCount = ArraySize(persistedIds);
      for(int e = 0; e < expectedCount; e++)
      {
         int idx = -1;
         for(int j = 0; j < persistedCount; j++)
         {
            if(persistedIds[j] == expectedIds[e]) { idx = j; break; }
         }
         if(idx < 0)
         {
            fetchVerified = false;
            verifyDetail += "missing:" + expectedIds[e] + " ";
         }
         else if(persistedHashes[idx] != expectedHashes[e])
         {
            fetchVerified = false;
            verifyDetail += "altered:" + expectedIds[e] + " ";
         }
      }
   }

   int reopened = FileOpen(OutputFile, FILE_READ | FILE_WRITE | FILE_BIN);
   bool seekOk = false;
   if(reopened != INVALID_HANDLE)
   {
      ulong endPos = FileSize(reopened);
      seekOk = FileSeek(reopened, (long)endPos, SEEK_SET);
      if(!seekOk)
      {
         // Never leave a handle in an unsafe (open but incorrectly
         // positioned) state for the caller to inherit (seventh review,
         // defect B): a subsequent window's write through this handle
         // would land at whatever position the failed seek left it at
         // -- empirically position 0, i.e. the START of the file, on
         // this build (see FileModeProbe.mq5) -- silently overwriting
         // the header and earliest rows. Close and invalidate it here,
         // immediately, so the caller's `fileHandle == INVALID_HANDLE`
         // check is authoritative.
         FileClose(reopened);
         reopened = INVALID_HANDLE;
      }
   }
   fileHandle = reopened;  // INVALID_HANDLE whenever open OR seek failed -- never a live-but-misplaced handle

   if(!rescanOk || !fetchVerified || fileHandle == INVALID_HANDLE)
   {
      string reason = !rescanOk
         ? "could not rescan/reopen/seek"
         : (!fetchVerified
            ? ("fetched record(s) missing or altered after persistence: " + verifyDetail)
            : "could not reopen/seek");
      PrintFormat("[MQL5 Exporter] FAILED window %s -> %s: %s %s -- NOT logging success",
                  TimeToString(winStart, TIME_DATE), TimeToString(winEnd, TIME_DATE), reason, OutputFile);
      failedWindows += TimeToString(winStart, TIME_DATE) + " to " + TimeToString(winEnd, TIME_DATE) + " (verification failed); ";
      LogWindowStatus(winStart, winEnd, "FAILED", 0, attempts, "");
      return 0;
   }

   // Checkpoint persistence failure must not become a successful
   // outcome (seventh review, defect A): the data was genuinely
   // fetched, written, and verified (canonicalCount/digest are real),
   // but if the "OK" evidence itself fails to persist, this window is
   // reported as FAILED for this run's summary -- not silently
   // announced durable when it is not yet durable. Nothing is lost: the
   // CSV rows remain, and a resume will simply re-fetch/re-log this
   // window, converging safely via RescanWindowDigest's canonical dedup.
   bool logged = LogWindowStatus(winStart, winEnd, "OK", canonicalCount, attempts, digest);
   if(!logged)
   {
      PrintFormat("[MQL5 Exporter] FAILED window %s -> %s: %d row(s) fetched and verified, but the "
                  "checkpoint could not be persisted -- NOT durable, will retry next run",
                  TimeToString(winStart, TIME_DATE), TimeToString(winEnd, TIME_DATE), rows);
      failedWindows += TimeToString(winStart, TIME_DATE) + " to " + TimeToString(winEnd, TIME_DATE) + " (checkpoint persistence failed); ";
      return 0;
   }

   totalRows += rows;
   PrintFormat("[MQL5 Exporter] %s -> %s : %d row(s) fetched, %d canonical row(s) in window",
               TimeToString(winStart, TIME_DATE), TimeToString(winEnd, TIME_DATE), rows, canonicalCount);
   return rows;
}

//+------------------------------------------------------------------+
//| Script entry point                                                 |
//|                                                                    |
//| FILE HANDLE OWNERSHIP (fifth review fix): resume verification      |
//| (WindowAlreadyOk, which reads OutputFile) now runs for EVERY        |
//| window BEFORE any writer is ever opened -- not interleaved with a  |
//| writer held open across the whole loop as before. A writer is      |
//| opened ONLY if at least one window actually needs fetching, and     |
//| only for the duration of the fetch pass.                            |
//+------------------------------------------------------------------+
void OnStart()
{
   // EndDate is INCLUSIVE (see the input declaration above) -- convert
   // it ONCE, here, to the exclusive midnight boundary used by every
   // window computation below.
   datetime endExclusive = NextDayStart(DayStart(EndDate));

   if(StartDate >= endExclusive)
   {
      Print("[MQL5 Exporter] ERROR: StartDate must be on or before EndDate");
      return;
   }

   WindowLogFile = OutputFile + ".windows.v" + EXPORT_SCHEMA_VERSION + ".csv";

   // Three-way outcome -- MIGRATION_ERROR is checked FIRST and
   // unconditionally aborts: no calendar data is requested, no CSV is
   // opened for append, and no checkpoint evidence is written.
   ENUM_MIGRATION_OUTCOME migrationOutcome = MigrateLegacyCsvIfNeeded();
   if(migrationOutcome == MIGRATION_ERROR)
   {
      Print("[MQL5 Exporter] ABORTING: could not safely prepare the output file (see error above). "
            "No calendar data was requested and no checkpoint evidence was written this run.");
      return;
   }
   bool needFreshFile = (migrationOutcome == MIGRATION_READY_NEW);

   // INTERRUPTED-WRITE TAIL RECOVERY (eighth review, P1): run BEFORE
   // anything reads or writes OutputFile/WindowLogFile for this run --
   // Pass 1's WindowAlreadyOk calls (via RescanWindowDigest) read
   // OutputFile, and WindowAlreadyOk itself reads WindowLogFile, so a
   // dangling fragment left by a PRIOR run's interruption must be
   // resolved first, or those reads would operate on a boundary an
   // append hasn't safely established yet. A no-op when either file
   // doesn't exist yet (a fresh setup, or one just migrated aside) or
   // already ends cleanly. See CalendarExporterCore.mqh's
   // RecoverFileTail for the full recovery procedure (never an
   // unprotected truncate-and-rewrite: any dropped bytes go through a
   // verified temp-file swap, and a merely newline-incomplete final
   // record is repaired by appending the single missing byte).
   if(RecoverFileTail(OutputFile, true) == TAIL_RECOVERY_ERROR)
   {
      Print("[MQL5 Exporter] ABORTING: could not safely establish an append boundary for ", OutputFile,
            " (see error above). No calendar data was requested and nothing was appended this run.");
      return;
   }
   if(RecoverFileTail(WindowLogFile, false) == TAIL_RECOVERY_ERROR)
   {
      Print("[MQL5 Exporter] ABORTING: could not safely establish an append boundary for ", WindowLogFile,
            " (see error above). No calendar data was requested and nothing was appended this run.");
      return;
   }

   // -- PASS 1: decide skip vs. fetch for every window, WITHOUT ever
   // opening a writer. A fresh (not-yet-existing) file trivially has
   // nothing to verify, so every window needs fetching in that case --
   // WindowAlreadyOk is skipped entirely rather than called against
   // content that cannot exist yet.
   datetime pendingStarts[];
   datetime pendingEnds[];
   int pendingCount = 0;
   int skippedWindows = 0;

   datetime cursor = MonthStart(StartDate);
   while(cursor < endExclusive)
   {
      datetime windowEnd = NextMonthStart(cursor);
      if(windowEnd > endExclusive) windowEnd = endExclusive;
      datetime windowStart = (cursor < StartDate) ? StartDate : cursor;

      bool alreadyOk = (!needFreshFile) && WindowAlreadyOk(windowStart, windowEnd);
      if(alreadyOk)
      {
         skippedWindows++;
      }
      else
      {
         pendingCount++;
         ArrayResize(pendingStarts, pendingCount);
         ArrayResize(pendingEnds, pendingCount);
         pendingStarts[pendingCount - 1] = windowStart;
         pendingEnds[pendingCount - 1] = windowEnd;
      }

      cursor = NextMonthStart(cursor);
   }

   if(pendingCount == 0)
   {
      PrintFormat("[MQL5 Exporter] DONE. Nothing to fetch -- all %d window(s) already verified OK. Output: MQL5\\Files\\%s",
                  skippedWindows, OutputFile);
      return;  // no writer ever opened -- resume-when-nothing-changed touches the file not at all
   }

   // -- PASS 2: open the writer ONCE and fetch only the pending windows.
   int fileHandle;
   bool seekOk = true;
   if(!needFreshFile)
   {
      // Append mode: re-running the exporter for a wider/later range (or
      // to retry failed months) must NOT truncate previously exported
      // months.
      fileHandle = FileOpen(OutputFile, FILE_READ | FILE_WRITE | FILE_BIN);
      if(fileHandle != INVALID_HANDLE)
      {
         ulong endPos = FileSize(fileHandle);
         seekOk = FileSeek(fileHandle, (long)endPos, SEEK_SET);
      }
   }
   else
   {
      fileHandle = FileOpen(OutputFile, FILE_WRITE | FILE_BIN);
   }

   if(fileHandle == INVALID_HANDLE || !seekOk)
   {
      PrintFormat("[MQL5 Exporter] ERROR: could not open/seek %s for writing (error %d) -- ABORTING",
                  OutputFile, GetLastError());
      if(fileHandle != INVALID_HANDLE)
      {
         FileClose(fileHandle);
         fileHandle = INVALID_HANDLE;  // defensive: never leave a stale handle value lying around
      }
      return;
   }

   if(needFreshFile)
   {
      if(!WriteUtf8Line(fileHandle, CSV_HEADER))
      {
         PrintFormat("[MQL5 Exporter] ERROR: could not write the header to %s -- ABORTING", OutputFile);
         FileClose(fileHandle);
         fileHandle = INVALID_HANDLE;
         return;
      }
      FileFlush(fileHandle);
   }

   int totalRows = 0;
   string failedWindows = "";

   for(int i = 0; i < pendingCount; i++)
   {
      if(fileHandle == INVALID_HANDLE)
      {
         // A prior iteration's reopen/seek failed inside FetchMonthWindow
         // -- never continue writing through an invalid handle.
         failedWindows += TimeToString(pendingStarts[i], TIME_DATE) + " to " +
                           TimeToString(pendingEnds[i], TIME_DATE) + " (skipped: no valid file handle); ";
         continue;
      }
      FetchMonthWindow(pendingStarts[i], pendingEnds[i], fileHandle, totalRows, failedWindows);
   }

   if(fileHandle != INVALID_HANDLE)
      FileClose(fileHandle);

   PrintFormat("[MQL5 Exporter] DONE. Total rows this run: %d. Skipped (already OK): %d. Output: MQL5\\Files\\%s",
               totalRows, skippedWindows, OutputFile);
   if(StringLen(failedWindows) > 0)
      PrintFormat("[MQL5 Exporter] FAILED WINDOWS (re-run to retry -- already-OK windows will be skipped): %s", failedWindows);
}
//+------------------------------------------------------------------+
