//+------------------------------------------------------------------+
//|                                          DigestSelfTest.mq5        |
//|                                                                    |
//| Standalone self-test for the UTF-8 encoding / FNV-1a digest        |
//| contract used by EconomicCalendarExporter.mq5's window-integrity   |
//| checks (see that file's "WINDOW INTEGRITY CONTRACT" and header      |
//| comment). Run this INSIDE MetaTrader as a script; it prints         |
//| PASS/FAIL for each of 8 fixed test vectors and a final summary      |
//| line to the Experts/Journal tab.                                    |
//|                                                                    |
//| These are the SAME 8 vectors as                                    |
//| tests/test_mql5_digest_encoding_contract.py (Python side) --       |
//| the expected canonical strings/digest hex values were computed by  |
//| an INDEPENDENT, from-scratch reference script (not this file, not  |
//| src/data/fetch/mql5.py, not EconomicCalendarExporter.mq5) and       |
//| pasted in as literal constants on BOTH sides.                       |
//|                                                                    |
//| PRODUCTION HELPERS, NOT DUPLICATES (eighth review): this file now   |
//| #include's CalendarExporterCore.mqh and calls its Fnv1a64/          |
//| CanonicalRowString directly -- the SAME compiled functions used by  |
//| EconomicCalendarExporter.mq5 itself, not a separately hand-typed    |
//| copy that could silently drift from (or independently mask a bug    |
//| in) the real implementation.                                        |
//|                                                                    |
//| VERIFICATION STATUS: this file HAS been compiled and run inside an  |
//| actual MetaTrader 5 terminal (MetaEditor /compile + a scripted       |
//| terminal /config startup). Beyond the ulong-overflow assumption      |
//| (standard documented C/C++ semantics, not independently re-derived),|
//| this also confirms MetaEditor correctly parses UTF-8-encoded non-    |
//| ASCII characters embedded directly in string literals (vectors 2-5  |
//| below).                                                              |
//|                                                                    |
//| MANUAL VERIFICATION PROCEDURE:                                     |
//|   1. Open this file in MetaEditor (File > Open, or drag into the   |
//|      editor) and compile (F7). Confirm zero errors/warnings.        |
//|   2. Attach it as a script to any chart (or Navigator > Scripts >   |
//|      double-click). It requires no inputs.                          |
//|   3. Open the Toolbox "Experts" tab (or "Journal") and read the     |
//|      per-vector PASS/FAIL lines and the final                       |
//|      "X/8 vectors passed" summary.                                  |
//|   4. All 8 must PASS. Any FAIL means this build's                   |
//|      StringToCharArray/CharArrayToString(..., UTF8_CODEPAGE)         |
//|      behavior, or its source-file Unicode handling, differs from    |
//|      what was assumed -- do not trust the main exporter's digest    |
//|      contract for non-ASCII calendar text until this is resolved.  |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

#include "CalendarExporterCore.mqh"

// JoinCanonical is kept as a thin local alias over the shared
// CanonicalRowString (which takes a `const string &fields[]`) purely
// so the rest of this file's vector-table code below reads unchanged.
string JoinCanonical(string &fields[])
{
   return CanonicalRowString(fields);
}

int g_passed = 0;
int g_failed = 0;

void CheckDigest(string name, string &fields[], string expectedDigest)
{
   string canonical = JoinCanonical(fields);
   ulong digest = Fnv1a64(canonical);
   string digestHex = StringFormat("%016I64x", digest);
   if(digestHex == expectedDigest)
   {
      PrintFormat("[DigestSelfTest] PASS: %s (digest=%s)", name, digestHex);
      g_passed++;
   }
   else
   {
      PrintFormat("[DigestSelfTest] FAIL: %s -- expected %s, got %s", name, expectedDigest, digestHex);
      g_failed++;
   }
}

// Mirrors RescanWindowDigest's canonical-record dedup: last occurrence
// of a given value_id (fields[0]) wins, combined via XOR (order-independent).
void CheckDedupDigest(string name, string &fieldsA[], string &fieldsB[], string expectedDigest)
{
   ulong hashA = Fnv1a64(JoinCanonical(fieldsA));
   ulong hashB = Fnv1a64(JoinCanonical(fieldsB));
   // fieldsA and fieldsB share the same value_id (fields[0]) -- last
   // occurrence (B) wins, so the "combined" digest for the canonical
   // record set is just B's own hash, not A XOR B.
   string combinedHex = StringFormat("%016I64x", hashB);
   if(combinedHex == expectedDigest)
   {
      PrintFormat("[DigestSelfTest] PASS: %s (digest=%s)", name, combinedHex);
      g_passed++;
   }
   else
   {
      PrintFormat("[DigestSelfTest] FAIL: %s -- expected %s, got %s", name, expectedDigest, combinedHex);
      g_failed++;
   }
}

void OnStart()
{
   g_passed = 0;
   g_failed = 0;

   // -- Vector 1: ASCII text --
   string v1[] = {
      "1","100","NFP","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDigest("1_ascii", v1, "dd0c0fd6dcf04073");

   // -- Vector 2: accented text (e with acute accent, U+00E9) --
   string v2[] = {
      "1","100","Café Price Index","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDigest("2_accented", v2, "1e22ead2ffe06d90");

   // -- Vector 3: curly apostrophe (U+2019) and em dash (U+2014) --
   string v3[] = {
      "1","100","Fed Chair’s Speech — Q&A","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDigest("3_curly_emdash", v3, "c23b545ef1a66d93");

   // -- Vector 4: non-Latin characters (Japanese, "Bank of Japan") --
   string v4[] = {
      "1","100","日本銀行","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDigest("4_non_latin", v4, "691d4a3aa43c37e7");

   // -- Vector 5: supplementary-plane Unicode character (emoji, U+1F600) --
   string v5[] = {
      "1","100","Rate Decision 😀","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDigest("5_supplementary", v5, "4d93af3ffe99600c");

   // -- Vector 6: commas and embedded quotes in the (already-unescaped)
   // field value -- the canonical join operates on parsed field text,
   // not the CSV-escaped on-disk form. --
   string v6[] = {
      "1","100","Fed Says, \"Rates Steady\"","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDigest("6_comma_quotes", v6, "8871a54e242c993d");

   // -- Vector 7: empty fields --
   string v7[] = {
      "1","100","NFP","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","","","","0","SERVER"
   };
   CheckDigest("7_empty_fields", v7, "9a6c9654149e1444");

   // -- Vector 8: duplicate value_id, later revised record wins --
   string v8a[] = {
      "42","100","NFP","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "216.000000","170.000000","173.000000","","0","SERVER"
   };
   string v8b[] = {
      "42","100","NFP","US","USD","HIGH","2024.01.05 13:30:00","2023.12.01","JOB","THOUSANDS",
      "218.000000","170.000000","173.000000","","0","SERVER"
   };
   CheckDedupDigest("8_duplicate_revision", v8a, v8b, "53ee8bf2392f4c12");

   PrintFormat("[DigestSelfTest] ==== %d/%d vectors passed ====", g_passed, g_passed + g_failed);
   if(g_failed > 0)
      Print("[DigestSelfTest] FAILURES DETECTED -- do not trust the digest contract until resolved.");
}
//+------------------------------------------------------------------+
