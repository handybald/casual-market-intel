//+------------------------------------------------------------------+
//|                                            FileModeProbe.mq5       |
//| One-off empirical probe (NOT part of the shipped pipeline) used    |
//| to determine real FileOpen/FileSeek/FileWrite semantics on this    |
//| actual MetaTrader build, rather than relying on documentation      |
//| recall. Writes PASS/FAIL/OBSERVED lines to the Experts log.        |
//| Safe: touches only its own dedicated test files under MQL5\Files\, |
//| never us_macro_calendar.csv or anything the real exporter uses.    |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

void OnStart()
{
   string path = "probe_filemode_test.bin";
   if(FileIsExist(path))
      FileDelete(path);

   // -- Q1: does FILE_READ|FILE_WRITE|FILE_BIN CREATE a missing file? --
   int h1 = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
   PrintFormat("[Probe] Q1 FileOpen(FILE_READ|FILE_WRITE|FILE_BIN) on MISSING file -> handle=%d exists_after=%s",
               h1, FileIsExist(path) ? "true" : "false");
   if(h1 != INVALID_HANDLE)
   {
      long pos1 = FileTell(h1);
      PrintFormat("[Probe] Q1b initial FileTell() on newly-created file = %d", pos1);
      FileClose(h1);
   }

   // -- Q2: seed the file with known content via FILE_WRITE (fresh), then
   // reopen with FILE_READ|FILE_WRITE and see whether content survives --
   int h2 = FileOpen(path, FILE_WRITE | FILE_BIN);
   uchar seed[5] = {'H','E','L','L','O'};
   FileWriteArray(h2, seed, 0, 5);
   FileClose(h2);
   PrintFormat("[Probe] Q2 seeded %s with 5 bytes, size now = %d", path, (int)FileSize2(path));

   int h3 = FileOpen(path, FILE_READ | FILE_WRITE | FILE_BIN);
   long sizeAfterReopen = (h3 != INVALID_HANDLE) ? (long)FileSize(h3) : -1;
   long posAfterReopen = (h3 != INVALID_HANDLE) ? FileTell(h3) : -1;
   PrintFormat("[Probe] Q2b reopen with FILE_READ|FILE_WRITE|FILE_BIN -> handle=%d size=%d initial_pos=%d "
               "(if size==5, FILE_READ|FILE_WRITE does NOT truncate; if size==0, it DOES)",
               h3, sizeAfterReopen, posAfterReopen);
   if(h3 != INVALID_HANDLE)
      FileClose(h3);

   // -- Q3: does bare FILE_WRITE|FILE_BIN on an EXISTING file truncate it? --
   int h4 = FileOpen(path, FILE_WRITE | FILE_BIN);
   long sizeRightAfterWriteOpen = (h4 != INVALID_HANDLE) ? (long)FileSize(h4) : -1;
   PrintFormat("[Probe] Q3 FileOpen(FILE_WRITE|FILE_BIN) on a 5-byte EXISTING file -> handle=%d "
               "size_immediately_after_open=%d (0 means FILE_WRITE alone truncates on open)",
               h4, sizeRightAfterWriteOpen);
   if(h4 != INVALID_HANDLE)
      FileClose(h4);

   // -- Q4: FileSeek behavior -- seek past EOF, seek with SEEK_SET to a
   // valid position, and confirm FileTell reflects it. --
   int h5 = FileOpen(path, FILE_WRITE | FILE_BIN);
   uchar seed2[10] = {'A','B','C','D','E','F','G','H','I','J'};
   FileWriteArray(h5, seed2, 0, 10);
   bool seekOk1 = FileSeek(h5, 4, SEEK_SET);
   long posAfterSeek = FileTell(h5);
   PrintFormat("[Probe] Q4 FileSeek(h,4,SEEK_SET) after 10-byte write -> returned=%s FileTell=%d",
               seekOk1 ? "true" : "false", posAfterSeek);
   FileClose(h5);

   // -- Q5: what does FileWrite (var-arg text form) do to file position
   // immediately after FileOpen(FILE_READ|FILE_WRITE|FILE_CSV) on an
   // EXISTING non-empty file, with NO explicit seek -- does it append
   // at EOF or overwrite from position 0? This is the exact ambiguity
   // that matters for LogWindowStatus's fix. --
   string csvPath = "probe_filemode_test.csv";
   if(FileIsExist(csvPath))
      FileDelete(csvPath);
   int h6 = FileOpen(csvPath, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   FileWrite(h6, "line1");
   FileWrite(h6, "line2");
   FileClose(h6);

   int h7 = FileOpen(csvPath, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   long posNoSeek = (h7 != INVALID_HANDLE) ? FileTell(h7) : -1;
   long sizeNoSeek = (h7 != INVALID_HANDLE) ? (long)FileSize(h7) : -1;
   FileWrite(h7, "line3-no-explicit-seek");
   FileClose(h7);

   string finalContent = "";
   int h8 = FileOpen(csvPath, FILE_READ | FILE_TXT | FILE_ANSI);
   while(h8 != INVALID_HANDLE && !FileIsEnding(h8))
      finalContent += FileReadString(h8) + " | ";
   if(h8 != INVALID_HANDLE) FileClose(h8);
   PrintFormat("[Probe] Q5 reopen(FILE_READ|FILE_WRITE|FILE_CSV) with NO seek: initial_pos=%d size=%d, "
               "final file content after appending 'line3' = [%s]",
               posNoSeek, sizeNoSeek, finalContent);

   // cleanup
   FileDelete(path);
   FileDelete(csvPath);
   Print("[Probe] DONE");
}

// FileSize requires an open handle in real MQL5 -- this helper opens
// read-only just to query size, for logging convenience only.
long FileSize2(string path)
{
   int h = FileOpen(path, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE) return -1;
   long sz = (long)FileSize(h);
   FileClose(h);
   return sz;
}
//+------------------------------------------------------------------+
