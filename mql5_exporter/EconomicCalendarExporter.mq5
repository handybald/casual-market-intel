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
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

input datetime StartDate     = D'2016.01.01 00:00:00';
input datetime EndDate       = D'2026.09.10 00:00:00';
input string   CountryCode   = "US";
input string   CurrencyCode  = "USD";
input string   OutputFile    = "us_macro_calendar.csv"; // written under MQL5\Files\

// The MQL5 Calendar API can time out or return incomplete data over very
// large ranges. Chunk internally, one calendar month at a time -- the
// user-facing inputs above stay a single wide range.
#define CHUNK_DAYS_APPROX 31

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

string EscapeCsv(string value)
{
   // Minimal CSV escaping: wrap in quotes if it contains a comma/quote,
   // and double any embedded quotes.
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

string DoubleOrEmpty(double v, bool isSet)
{
   if(!isSet) return "";
   return DoubleToString(v, 6);
}

//+------------------------------------------------------------------+
//| Fetch one calendar-month window, with retry on empty/failed calls |
//+------------------------------------------------------------------+
int FetchMonthWindow(datetime winStart, datetime winEnd, int fileHandle, int &totalRows, string &failedWindows)
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
      return 0;
   }

   int rows = ArraySize(values);
   for(int i = 0; i < rows; i++)
   {
      MqlCalendarEvent ev;
      MqlCalendarCountry ctry;
      string eventName = "";
      string unit = "";
      if(CalendarEventById(values[i].event_id, ev))
      {
         eventName = ev.name;
         unit = ev.unit;
      }
      string countryCodeOut = CountryCode;
      if(CalendarCountryById(ev.country_id, ctry))
         countryCodeOut = ctry.code;

      // values[i].actual_value/forecast_value/prev_value/revised_prev_value are
      // stored as integers scaled by 10^digits (LONG_MIN means "not set").
      // Rescale using the event's digits before writing the human value out.
      double scale = MathPow(10, ev.digits);

      // NOTE ON TIMEZONE: values[i].time comes from the terminal/broker
      // environment. We do NOT assert this is UTC -- record it as-is and
      // let the Python side treat it as "SERVER" origin, to be reconciled
      // explicitly rather than silently assumed. See src/data/timeutil.py.
      string line = StringFormat(
         "%I64u,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,SERVER",
         values[i].event_id,
         EscapeCsv(eventName),
         countryCodeOut,
         CurrencyCode,
         ImportanceToString(ev.importance),
         TimeToString(values[i].time, TIME_DATE | TIME_SECONDS),
         EscapeCsv(unit),
         DoubleOrEmpty(values[i].actual_value / scale, values[i].actual_value != LONG_MIN),
         DoubleOrEmpty(values[i].forecast_value / scale, values[i].forecast_value != LONG_MIN),
         DoubleOrEmpty(values[i].prev_value / scale, values[i].prev_value != LONG_MIN),
         DoubleOrEmpty(values[i].revised_prev_value / scale, values[i].revised_prev_value != LONG_MIN)
      );
      FileWrite(fileHandle, line);
   }

   totalRows += rows;
   PrintFormat("[MQL5 Exporter] %s -> %s : %d rows",
               TimeToString(winStart, TIME_DATE), TimeToString(winEnd, TIME_DATE), rows);
   return rows;
}

//+------------------------------------------------------------------+
//| Script entry point                                                |
//+------------------------------------------------------------------+
void OnStart()
{
   if(StartDate >= EndDate)
   {
      Print("[MQL5 Exporter] ERROR: StartDate must be before EndDate");
      return;
   }

   int fileHandle = FileOpen(OutputFile, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(fileHandle == INVALID_HANDLE)
   {
      PrintFormat("[MQL5 Exporter] ERROR: could not open %s (error %d)", OutputFile, GetLastError());
      return;
   }

   FileWrite(fileHandle,
      "event_id,event_name,country_code,currency_code,importance,event_time,unit,actual_value,forecast_value,prev_value,revised_prev_value,source_timezone");

   int totalRows = 0;
   string failedWindows = "";

   // Internal monthly chunking -- the user only ever specifies StartDate/EndDate.
   datetime cursor = MonthStart(StartDate);
   while(cursor < EndDate)
   {
      datetime windowEnd = NextMonthStart(cursor);
      if(windowEnd > EndDate) windowEnd = EndDate;
      datetime windowStart = (cursor < StartDate) ? StartDate : cursor;

      FetchMonthWindow(windowStart, windowEnd, fileHandle, totalRows, failedWindows);

      cursor = NextMonthStart(cursor);
   }

   FileClose(fileHandle);

   PrintFormat("[MQL5 Exporter] DONE. Total rows: %d. Output: MQL5\\Files\\%s", totalRows, OutputFile);
   if(StringLen(failedWindows) > 0)
      PrintFormat("[MQL5 Exporter] FAILED WINDOWS (re-run to retry): %s", failedWindows);
}
//+------------------------------------------------------------------+
