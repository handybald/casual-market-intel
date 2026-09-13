# Causal Market Intelligence — Historical Data Ingestion

Research infrastructure for building a raw + normalized data foundation
(macro calendar events, market bars, official validation series) ahead
of later causal-inference / information-theoretic / multi-agent
modeling work. This repo is **ingestion, storage, normalization,
validation, and incremental updates only** — no models, agents,
databases, or orchestration infrastructure.

## Data sources

| Source | Role | Credential |
|---|---|---|
| MQL5 Economic Calendar | event metadata, `actual`/`previous`/`revised_previous`; `forecast` is diagnostic only | none (manual MetaTrader export, see below) |
| Forex Factory | canonical pre-release forecast (`provider_forecast`, never called `economist_consensus`) | none — public HTTP scrape |
| Massive | consolidated US-equity 1-minute market data (QQQ/SPY, configurable) | `MASSIVE_API_KEY` |
| FRED | official observations for validation/revision history — never a forecast source | `FRED_API_KEY` |
| BLS | real adapter, deliberately configured with **0 series by default** (FRED already covers the currently-mapped indicators) | `BLS_API_KEY` (optional) |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in MASSIVE_API_KEY / FRED_API_KEY
```

Python 3.9+ (the repo avoids 3.10-only syntax on purpose;
`pandas_market_calendars` is pinned to `4.6.1` for that reason).

## Commands

```bash
# One-time historical bootstrap (single date range; each provider
# chunks/paginates/retries/resumes internally)
python scripts/fetch_historical_data.py --start 2016-01-01 --end 2026-09-10

# Re-run the same command any time to resume — already-verified chunks
# are skipped, failed/provisional ones are retried automatically.
python scripts/fetch_historical_data.py --start 2016-01-01 --end 2026-09-10

# Scheduled incremental update (no date args needed)
python scripts/update_data.py

# Read-only validation report (cross-source macro comparison + OHLCV/session checks)
python scripts/validate_data.py
```

Both `fetch_historical_data.py` and `update_data.py` exit non-zero if
any requested source failed, is missing credentials, still has a
coverage gap, or (`update_data.py`) still has an unresolved gap — a
successful refresh whose only "gap" is today's still-provisional data
exits 0; a gap backed by an actual failure or nothing at all does not.
`--sources` rejects unknown names outright rather than silently doing
nothing. `validate_data.py` exits non-zero on a **hard** data-integrity
failure (invalid OHLCV, missing sessions, etc.) — a cross-source
`MISMATCH` alone does not fail the run, since surfacing disagreement is
the point of running it.

### Small CPI/NFP smoke test (recommended before a full backfill)

```bash
python scripts/fetch_historical_data.py --sources fred --start 2024-01-01 --end 2024-03-31
python scripts/fetch_historical_data.py --sources massive --symbols QQQ --start 2024-01-01 --end 2024-01-10
python scripts/validate_data.py
```

The first line fetches CPI/NFP/unemployment/AHE FRED series for one
quarter (requires `FRED_API_KEY`); the second fetches ten days of QQQ
1-minute bars (requires `MASSIVE_API_KEY` on a plan that includes range
aggregates — see Known limitations). Confirm both report `ok: True`
before running a multi-year backfill.

## The MQL5 step is manual

The MQL5 Calendar API only runs inside the MetaTrader 5 terminal — there
is no HTTP endpoint, so nothing here can automate it. `mql5_exporter/
EconomicCalendarExporter.mq5` is the piece you run **inside MetaTrader**:

1. Open MetaEditor, load `mql5_exporter/EconomicCalendarExporter.mq5`, compile.
2. Run it as a script (right-click → attach to a chart), set `StartDate`/`EndDate`/`CountryCode`/`CurrencyCode`. **Both dates are inclusive** — "`EndDate = 2024.01.31`" means data through Jan 31 itself, matching the `--start`/`--end` convention every other `fetch_historical_data.py --sources ...` invocation already uses. (A prior version of this doc/script pair required `EndDate` to already be "the day after the last day you want" — that inconsistency has been fixed; see the exporter's own input-declaration comments and `src/data/fetch/mql5.py`'s module docstring for the exact internal half-open representation.)
3. It writes `MQL5/Files/us_macro_calendar.csv` (chunking internally by month, with a durable per-window status log at `us_macro_calendar.csv.windows.v5.csv` keyed by exact window boundaries + country/currency + schema version, not just year/month) — copy or symlink both files to `data/raw/mql5/`. A window-log "OK" entry alone is never sufficient: it is only trusted when the CSV's *current content* for that exact window still matches a per-window content digest recorded at successful export time (not a plain row count, a file creation date, or a whole-file checksum — see "MQL5 window integrity contract" below). Deleting `us_macro_calendar.csv` without also deleting its window-log sidecar is safe; the stale log entries will no longer be trusted. All CSV/log file I/O is UTF-8 (see below) — a calendar event name containing non-ASCII text (an accented character, etc.) is fully supported, never stripped or transliterated.
4. `fetch_historical_data.py --sources mql5` then ingests whatever's there, filtered to the requested range, and reports exactly which months are still missing. Pass the SAME inclusive `--start`/`--end` you gave the exporter — the two sides compute identical window boundaries for identical inclusive input, with no month-end special-casing on either side (see `tests/test_mql5_date_semantics_phase2a.py`).

### MQL5 window integrity contract (v5) and migrating from an older log

The window-log format has changed three times since it was introduced (v2 → v3 → v4 → v5). Each bump is a *breaking* format/encoding change, and by design an old-version log is simply **invisible** to the current code (a different filename), never partially parsed — so upgrading is always safe, at the cost of re-verifying (not re-fetching data you don't have — the CSV rows themselves are untouched) every window at least once:

- **v2 → v3** added a `csv_create_date` field (superseded below).
- **v3 → v4** replaces the plain row count + creation date with a **canonical row count + content digest** per window: the exporter hashes the exact set of rows (deduplicated by `value_id`, keeping the latest occurrence — see the module docstring in `src/data/fetch/mql5.py` for why physical vs. canonical rows matters) it exported for that window, using a small from-scratch 64-bit FNV-1a hash (deliberately not a cryptographic hash, so it needs no MQL5 crypto-library call — see the .mq5 file's own header comment). Ingestion recomputes the same digest from the CSV's *current* content and only certifies completion on an exact match. This catches header-only truncation, partial truncation, and same-row-count in-place edits — none of which the v3 (or the original v2-era row-presence-only) check could detect.
- **v4 → v5** makes the byte encoding explicit: CSV/log text is UTF-8 on both the Python and MQL5 sides, always. v4 (and earlier) used MQL5's `FILE_ANSI` text mode (the OS system codepage — e.g. Windows-1252, a *different* byte encoding than UTF-8) for all file I/O, and hashed `StringGetCharacter()` return values, which are UTF-16 code units, not UTF-8 bytes. For pure-ASCII text these coincide; for any non-ASCII character (an accented letter, a curly quote, non-Latin text, an emoji) they do not, so an intact export containing such text could fail integrity verification — or fail to even decode as UTF-8 — purely from this mismatch, not real corruption. v5 never re-certifies v4 (or earlier) evidence under the new byte-accurate algorithm, even for a file that happened to be pure ASCII (and thus byte-identical either way) — re-export is required regardless, so no old evidence is ever silently trusted under a different algorithm. See `tests/test_mql5_digest_encoding_contract.py` for fixed test vectors (ASCII, accented text, curly quotes/em dash, non-Latin, a supplementary-plane emoji, embedded commas/quotes, empty fields, a duplicate-ID revision) with independently-computed expected canonical bytes and digests, and `mql5_exporter/DigestSelfTest.mq5` for the MQL5-side self-test consuming the same vectors.
- **Encoding compatibility check (fixed, seventh review):** an earlier version of `MigrateLegacyCsvIfNeeded()` decided whether an existing `us_macro_calendar.csv` was safe to append to by comparing only its *decoded* header text against the current schema's header — but the v4 and v5 headers are byte-identical, pure-ASCII text, so this check could never actually tell a v4 ANSI file apart from a genuine v5 UTF-8 file. A v4 file whose *data rows* contained non-ASCII bytes (which are not valid UTF-8 on their own) could be silently accepted and receive freshly appended UTF-8 rows underneath its differently-encoded old data. The fix validates the file's **raw bytes** against RFC 3629 (a from-scratch `IsValidUtf8`, mirroring the digest algorithm's own no-library-dependency design) *before* the header is ever compared; a file that fails byte-level validation is always moved aside as `.legacy_*.csv` (byte-for-byte preserved, untouched) and a fresh v5 file is started — the original codepage is never guessed, and no partial/best-effort conversion is attempted. Pure-ASCII legacy content is accepted (ASCII bytes mean the same thing under both encodings, so this is the one genuinely safe case). See `mql5_exporter/FileRecoverySelfTest.mq5` (scenarios 4–8) and `tests/test_mql5_checkpoint_persistence.py` for both the old (buggy) and fixed behavior side by side.
- **Window-log durability (fixed, seventh review):** the window-log's own write (`LogWindowStatus`) previously read the existing log into memory, then opened it with a bare `FileOpen(FILE_WRITE|FILE_BIN)` — empirically confirmed (`FileModeProbe.mq5`) to truncate the file immediately on open, before a single byte of the rewrite happened. Any interruption between that truncating open and the rewrite destroyed every prior checkpoint record, not just the one being added, and a failed *read* of the existing log was silently treated the same as "log absent" (same destructive consequence). The fix is a genuine durable append: `FileOpen(FILE_READ|FILE_WRITE|FILE_BIN)` (confirmed non-truncating) plus an explicit, checked `FileSeek` to end-of-file before writing — nothing is ever read back into memory or rewritten. A failure to persist the checkpoint now also propagates to the window's own outcome (never silently reported as `OK`); see `mql5_exporter/FileRecoverySelfTest.mq5` (scenarios 1–3) and `tests/test_mql5_checkpoint_persistence.py`.
- **Interrupted-write recovery, and fetched-vs-persisted verification (fixed, eighth review):** the durable-append fix above stops the window LOG from destroying history on interruption, but the CSV/log's own *content* could still end up with a dangling, non-newline-terminated final record if a write was interrupted between a row's content bytes and its trailing newline (or mid-way through the content itself — inside a quoted field, or inside a multibyte UTF-8 character). The NEXT run would then seek to end-of-file and append its first new row directly onto that fragment, producing one garbled, unparseable line that the rescan silently drops (by design, so one bad row can't fail an entire rescan) — a window could then be certified `OK` using a digest of only the rows that happened to survive. Two fixes, both required: (1) `RecoverFileTail`/`ComputeSafeAppendBoundary` run once at the start of every export, for both the CSV and the window-log, *before* anything else reads or writes either — a genuinely incomplete trailing record is dropped via a verified, never-in-place temp-file swap (the original is never opened in a truncating mode), while a *structurally complete* record missing only its newline is repaired by appending that single byte, never dropped; (2) `FetchMonthWindow` now cross-checks every record it actually fetched this run against the rescanned, persisted canonical set by id *and content* — a missing or altered record fails the window outright, rather than letting a merely self-consistent digest stand in for "the data I just fetched is actually there." A related interaction was also fixed: a whole-file UTF-8 validity check would previously reject an otherwise-perfectly-good v5 file wholesale just because its last write was cut off mid-character — the check is now truncation-tolerant, so only the incomplete tail (not the file's entire history) is ever migrated aside or dropped. See `mql5_exporter/TailRecoverySelfTest.mq5` (34/34 scenarios) and `tests/test_mql5_interrupted_write_recovery.py`.
- **Strict UTF-8 validation (fixed, eighth review):** `IsValidUtf8` previously checked lead-byte ranges and generic continuation-byte *shape* (`10xxxxxx`) but not the additionally-narrowed range certain lead bytes require, so it wrongly accepted `E0 80 80` (an overlong encoding of U+0000), `ED A0 80` (a UTF-16 surrogate half, U+D800, never a valid Unicode scalar value), and `F4 90 80 80` (decodes beyond U+10FFFF) — all three rejected by Python's own strict UTF-8 decoder. The fix narrows the first continuation byte's valid range for the E0/ED/F0/F4 lead bytes per the Unicode Standard's well-formedness table (RFC 3629 §4). Used everywhere raw bytes are trusted as UTF-8 — migration, append-boundary recovery, and (now) every read of the CSV/log content itself (`ReadUtf8File` validates before decoding, rather than relying on `CharArrayToString`'s own lenient handling of invalid input). See `mql5_exporter/FileRecoverySelfTest.mq5` (scenarios 9–35, cross-checked against every code point named above) and `tests/test_mql5_strict_utf8_validation.py`.

**To migrate:** nothing to do by hand. The next time you run the exporter, it will not find a `.windows.v5.csv` file, treat every window as unverified, and re-verify (re-request) them from the Calendar API — your existing `us_macro_calendar.csv` rows are untouched and are not re-fetched from scratch as new data, only re-confirmed. The next `fetch_historical_data.py --sources mql5` run will likewise stop trusting an old `.windows.v4.csv` (or earlier) log (it looks for `.windows.v5.csv` specifically) and fall back to the provisional row-presence heuristic until the exporter has produced a v5 log. Old `.windows.v2.csv`/`.windows.v3.csv`/`.windows.v4.csv` files are left on disk untouched (never deleted automatically) — safe to delete once you've confirmed the v5 log covers what you need, or keep them as an audit trail.

**This script has been compiled and run inside an actual MetaTrader 5
terminal** (via MetaEditor's `/compile` CLI and a scripted terminal
`/config` startup — see the exporter's own top-of-file "VERIFICATION
STATUS" comment, `mql5_exporter/FileModeProbe.mq5`,
`mql5_exporter/FileRecoverySelfTest.mq5`, and
`mql5_exporter/DigestSelfTest.mq5`, none of which are part of the
shipped pipeline). That confirmed the enum members used (`ev.unit`
values in particular — an earlier version referenced
`CALENDAR_UNIT_MOM/YOY/QOQ`, which the compiler rejected as
undeclared: they don't exist in the real `ENUM_CALENDAR_EVENT_UNIT`),
the UTF-8 round-trip and digest algorithm, and the exact
`FileOpen`/`FileSeek` truncation/positioning semantics the append and
migration fixes above depend on. **Not** yet verified:
`CalendarValueHistory`/`CalendarEventById`/`CalendarCountryById`
themselves (no live calendar data has been fetched in this
environment), `FileMove`'s exact signature (still documentation
recall), and `ulong` overflow wraparound (documented C/C++ semantics,
not independently re-derived). Re-running the exporter is safe: it
durably appends rather than truncates, both for the CSV data and for
its own window-log checkpoint file (after migrating any
incompatible-header-*or*-incompatible-encoding legacy file aside
instead of corrupting it), and skips only windows whose *exact*
boundaries were already logged `OK` — exporting Sept 1–10 and later
widening to Sept 1–30 correctly re-fetches the full month rather than
being skipped.

By default, MQL5 timestamps are **quarantined** (`release_timestamp_utc
= null`) because we have no reliable way to know the broker/server
timezone from Python. If you've confirmed your MT5 server's timezone,
set `providers.mql5.broker_timezone` in `config/data_sources.yaml` to
its IANA name (e.g. `"Europe/Helsinki"`).

Forex Factory timestamps default to `timestamp_quality=ASSUMED` (a
documented-but-unverified display-timezone default), not `CONFIRMED`.
Set `providers.forex_factory.display_timezone_verified: true` only
after independently confirming the display timezone for your fetch
setup — precision-sensitive checks (e.g. the minute-level macro-release
window check in `validate_data.py`) only trust `CONFIRMED` rows.

## Checkpoint / update strategy

`data/manifests/fetch_manifest.json` tracks one entry per
`(provider, key, start, end)` chunk attempted, with status `complete`,
`empty` (verified absence), `provisional` (fetched, but not yet safe to
treat as final — e.g. it touches "today"), or `failed`.

- **Watermark, not max-end**: `Manifest.completion_watermark()` only
  advances through *contiguous, verified* coverage from the dataset
  start. A failed or provisional chunk holds it back even if a later
  chunk succeeded — a failed July doesn't get silently skipped just
  because August worked. `Manifest.classify_gaps()` additionally tells
  a genuinely unresolved gap apart from one that's merely provisional
  (successfully collected, pending finalization) or in the future —
  what actually drives `update_data.py`'s exit code.
- **Provisional windows are always retried**: the current Forex Factory
  month and the trailing `revision_overlap_days` of Massive data are
  never checkpointed "complete" — they're re-fetched on every run to
  catch late prints, corrections, and revisions.
- **Validated before finalization, not just after sorting/dedup**: a
  Massive chunk is only checkpointed `complete` if it passes
  `validation.market.validate_market_bars` scoped to exactly that
  chunk's requested window (not the whole year) — non-finite/invalid
  OHLCV, unsorted/duplicate timestamps, or a missing NYSE session inside
  the requested window all block finalization. **Supported timeframes**:
  any intraday minute/hour timeframe expressible as a whole number of
  minutes (`"1min"`, `"5min"`, `"15min"`, `"1h"`, ...) — the fetcher and
  `scripts/validate_data.py` both derive the expected per-minute grid
  from the SAME `validation.market.timeframe_minutes_from_parts`, so
  they can never silently disagree about what a configured timeframe
  means. Daily (or coarser) bars and a zero/negative interval are
  explicitly rejected (`UnsupportedTimeframeError`) *before* any network
  call — this pipeline's completeness model is per-minute-intraday-
  session-based and does not (yet) implement a separate per-day
  completeness check, so a daily timeframe is refused rather than
  silently validated as if it were 1-minute bars. An empty response is
  only accepted as verified-`empty` if the window genuinely has zero
  expected NYSE sessions; an empty response for a normal trading window
  is `failed`, not silently accepted. Every validation outcome is
  persisted to `data/manifests/validation_reports/`.
- **Artifact integrity is checked, not assumed, and never blindly
  re-blessed**: before trusting a `complete`/`empty` entry, the manifest
  verifies the artifact file still exists and its checksum still
  matches. If a shared Massive year-file is missing or corrupt, *every*
  checkpoint backed by it is invalidated up front — not just the one
  chunk being re-fetched — and after a rewrite, each sibling checkpoint
  is individually re-verified against the rebuilt file's actual rows
  before its checksum is refreshed; one that no longer has its claimed
  data is invalidated instead of silently re-trusted.
- **Durability**: Massive writes/checkpoints each month immediately
  (not batched per year) — an interruption never loses already-fetched
  months. Forex Factory and FRED preserve every attempt (including
  failed ones) as its own immutable file, never overwriting a prior
  attempt, with a separate pointer to whichever one is currently trusted.
- **FRED (latest-revised path) is the exception to chunking**: it always
  re-fetches its full configured range (these series are small) rather
  than using the chunk/watermark machinery, to catch late-arriving
  observations and revisions to older periods. Each fetch is an
  immutable, separately timestamped snapshot; a response that's shorter
  than the last verified one — not just totally empty — is rejected and
  never becomes the new "latest verified" snapshot. A genuinely narrower
  follow-up request (a different, non-overlapping range) is not
  penalized for returning fewer rows.

`scripts/update_data.py` re-invokes the *same* fetch logic as bootstrap
across the full configured range — that's not wasteful, because the
manifest already skips anything durably verified. What actually runs is
exactly the gaps, failures, and provisional windows.

## Provenance

Every normalized row distinguishes **acquisition time** (when the
underlying raw artifact was actually fetched) from **normalization
time** (when this row was derived from it) — collapsing them was a bug
in an earlier version: a cached/skipped-cache Forex Factory month no
longer claims "acquired now", and Massive rows carry the acquisition
time of the *specific* month-chunk they came from, not a single
blanket value (e.g. the latest fetch) applied across an entire year
file. `raw_artifact_checksum` links a normalized row back to the exact
raw file it came from.

## Official validation: latest-revised vs. as-of vintage

FRED-derived official rows carry `official_vintage_kind`:

- `LATEST_REVISED` (the default, full-bootstrap path): today's
  best-known, possibly-revised value. Its `official_vintage_date` is
  when *that* revision became official — not a first-publication date.
- `AS_OF`: a real ALFRED vintage snapshot (`realtime_start=realtime_end=
  <date>`) — values exactly as they stood on one specific historical
  date. This is the historically-honest comparison against a calendar's
  `actual`, implemented and tested in `fetch/fred.py`
  (`fetch_observations_as_of`) / `normalize/fred.py`
  (`normalize_fred_asof_events`), but **not wired into the default
  bootstrap** — it's one extra API request per as-of date, which would
  be hundreds of extra requests across a decade of monthly releases.
  Call it directly, for a small, deliberately chosen set of historical
  release dates, via the documented command:

  ```
  python scripts/fetch_fred_asof.py --event-family CPI_MOM \
      --observation-start 2024-01-01 --observation-end 2024-01-31 \
      --as-of 2024-02-05
  ```

  `--event-family` is looked up against `providers.fred.official_series`
  in `config/data_sources.yaml` (the same config the default
  LATEST_REVISED fetch uses), and the normalized AS_OF event is merged
  into `data/interim/macro/fred_events.parquet` alongside (never
  overwriting) any LATEST_REVISED row for the same period.

`validation.macro.compare_macro_sources` produces SEPARATE results per
vintage: a plain cross-source `actual` comparison (e.g. MQL5 vs Forex
Factory), one `actual_vs_official_asof:<date>` result per distinct AS_OF
vintage date fetched (or an explicit `actual_vs_official_asof`
"unavailable" result when none exists yet for a period that otherwise
has official data), and a separate, clearly diagnostic-only
`actual_vs_official_latest_revised` result — so a later revision can
never retroactively turn a matching historical (AS_OF) comparison into
a mismatch, and multiple historical AS_OF snapshots for the same period
are all preserved rather than overwriting each other.

## Known limitations / deferred work

- **Massive contract is documentation-verified, not fully live-verified**: a real (plan-limited) API key confirmed the exact endpoint, auth parameter, and field schema (`t/o/h/l/c/v/vw/n`, `status`, error shape) live — see the module docstring in `src/data/fetch/massive.py` — but 1-minute range-aggregate retrieval itself was blocked by that key's plan tier, so a full historical bar fetch has not been exercised live.
- **MQL5 exporter: compiled and run, but never against live calendar data.** As of the eighth review, the exporter — and its four standalone self-tests (`DigestSelfTest.mq5`, `FileModeProbe.mq5`, `FileRecoverySelfTest.mq5`, `TailRecoverySelfTest.mq5`) — have actually been compiled (MetaEditor `/compile`) and executed (a scripted terminal `/config` startup) inside a real MetaTrader 5 terminal, all with 0 compiler errors/warnings and 100% self-test pass rates. See the exporter's own top-of-file "VERIFICATION STATUS" comment for the exact list of what that confirmed (real enum members, the UTF-8/digest round-trip, `FileOpen`/`FileSeek` truncation and positioning semantics, strict UTF-8 validation, and interrupted-write tail recovery). The self-tests call the exporter's own shared helpers directly via `mql5_exporter/CalendarExporterCore.mqh` — not hand-typed duplicates — so a passing self-test is evidence about the actual production code. This is genuine compiler/runtime evidence, not only the Python-simulation cross-check (`tests/_mql5_exporter_sim.py`, including an `ExclusiveFileHandleRegistry` that models "no two concurrently open handles to the same path"). **Still not exercised**: `CalendarValueHistory`/`CalendarEventById`/`CalendarCountryById` against a live MT5 terminal's actual calendar data — no calendar export, small or large, has been run in this environment, so end-to-end behavior against real API responses (as opposed to synthetic self-test fixtures) remains unverified. `FileMove`'s exact signature is now confirmed against official documentation (not memory), but its call sites are still only compiled, never exercised by a self-test. Manual verification procedure once you have MetaTrader access to a live calendar:
  1. **First export.** Run the main exporter once for a small range (e.g. `StartDate=2024.01.01`, `EndDate=2024.01.31`), confirm `us_macro_calendar.csv` and `us_macro_calendar.csv.windows.v5.csv` are created with real rows and one `OK` log line. If your calendar's country/currency selection includes any non-ASCII event names, open the CSV in a UTF-8-aware editor and confirm the text renders correctly (not mojibake).
  2. **Identical resume.** Re-run it completely unchanged — the log line "Skipped (already OK): 1" (or "Nothing to fetch -- all N window(s) already verified OK" if every window skips) should appear, and no new Calendar API request should be visible in the Experts log for that window. This is the resume-verification path fixed in the sixth review (see `test_mql5_file_handle_ownership.py`) — it must complete without any file-access error being logged.
  3. **Damaged-file repair.** Manually delete a few data rows from the CSV (simulating truncation) and re-run — that window must be re-fetched (not skipped), and a fresh `OK` line with a different digest should be appended. Then try modifying one value in place (e.g. change a printed `actual_value`) without changing the row count, and re-run — same expectation: re-fetched, not skipped.
  4. Rename the CSV's header row to something else and re-run — the log should show the migration/abort message and either a `.legacy_*.csv` file (successful migration) or a clean abort with the original file byte-for-byte unchanged (simulated failure), never a silent append.
- **FRED AS_OF vintage retrieval is implemented, tested, and usable via `python scripts/fetch_fred_asof.py`, but not part of the default bootstrap** (see above) — the default historical dataset uses LATEST_REVISED official values, clearly labeled as such, and validated as a separate diagnostic-only comparison, never silently conflated with a first-release comparison.
- **Cross-provider unit alignment is partial**: Forex Factory's own K/M/B/% suffixes are normalized to a consistent unit; MQL5's `unit`/`multiplier` enums are correctly separated and PERCENT-family units map to a comparable unit, but non-percent MQL5 units (job counts, dollar amounts, hours) are left `UNKNOWN` rather than guessed, since we can't verify MQL5's exact scaling convention for them without live access. `validation/macro.py` excludes any `UNKNOWN`-unit value from numerical comparison entirely (never a false `MATCH`/`MISMATCH` against a known-unit value), so this shows up as `MISSING`, not a wrong answer.
- **`reference_period` for MQL5 rows prefers `MqlCalendarValue.period`** (the real field) when the exporter CSV has it; Forex Factory and legacy MQL5 exports without a `period` column fall back to the "released the following month" heuristic (`src/data/reference_period.py`), which does not generalize to non-monthly release conventions.
- **BLS** (`src/data/fetch/bls.py`) is a real, tested adapter — chunking, response validation, immutable snapshots — deliberately configured with 0 series by default; add series IDs to `config/data_sources.yaml` `providers.bls.series` to use it. Not live-verified in this environment.
- **Market session validation** uses `pandas_market_calendars`' NYSE calendar (holidays, early closes), scoped to the exact requested window; it reports missing sessions and intraday gaps separately but does not attempt a fully automatic "no qualifying trade" vs. "collection failure" classifier beyond that.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

All tests run against fixtures/mocked transports — no credentials or
live network access required. See `tests/` for fixture provenance notes
where markup/API shape is asserted (e.g. `tests/fixtures/
forex_factory_sample.html`'s header comment).

**"Mocked transport" is not uniform across every test** — most fetch
tests (e.g. `tests/test_fetch_massive.py`'s pagination/auth coverage)
replace only `request_with_retry`, the actual HTTP call site, so real
URL/params construction and JSON response parsing run for real.
`tests/test_massive_timeframe_validation.py` explicitly separates its
two kinds: a `UNIT TESTS` section that replaces `fetch_window_bars`
directly (isolating timeframe/validation/manifest logic from the HTTP
layer entirely — it does NOT exercise request construction or response
parsing) from an `INTEGRATION TESTS` section that mocks only
`request_with_retry`. A prior version of that file mixed both under one
undifferentiated description; treat any test file's own section
headers/docstrings as authoritative over this summary.
