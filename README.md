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
| Alpaca (SIP) | **canonical** 1-minute market data: consolidated US-equity OHLC, volume, VWAP (QQQ/SPY, configurable); default market provider | `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY` (free Basic plan, live-verified) |
| Alpaca (IEX) | sparse single-venue sanity check only (`feed: "iex"`, stored separately) — **not** consolidated | same keys |
| Massive | validation/reference market provider (explicit `--sources massive`); not the canonical source | `MASSIVE_API_KEY` |
| FRED | official observations for validation/revision history — never a forecast source | `FRED_API_KEY` |
| BLS | real adapter, deliberately configured with **0 series by default** (FRED already covers the currently-mapped indicators) | `BLS_API_KEY` (optional) |

## Market data providers

### Source policy

| Quantity | Canonical source | Notes |
|---|---|---|
| 1-minute OHLC | **Alpaca SIP**, raw (unadjusted) | consolidated tape; default `providers.alpaca.feed: "sip"` |
| Volume | **Alpaca SIP** | provider volume definitions differ (see below); downstream volume features use ONE source and record it |
| VWAP | **Alpaca SIP** `vw` | Massive's `vw` is kept only as provider-native reference data, never mapped onto the same quantity |

- Massive is a **validation/reference** provider: historical overlap and
  implementation checks. It is not fetched by default.
- Alpaca IEX is an independent **sparse single-venue** sanity check, not a
  research dataset.
- Databento (future) would cover selected microstructure/event windows
  only (trades/BBO/L2/L3).
- The research benchmark is **2016-01-01 → present**.

**Evidence** (live, 2026-10-04, QQQ 1-minute raw; reports in `data/reports/`
and `data/manifests/validation_reports/`):
- Alpaca SIP on the current *free Basic* account served historical data
  for 2016-03-15, 2017-06-15, 2024-10-02, 2024-11-06, 2024-11-28 and
  2024-11-29:
  - full regular sessions every trading day, including the 09:30–13:00
    early close (210/210 minutes);
  - the Thanksgiving holiday returned an empty response, recorded as
    verified-empty;
  - multi-page pagination works.
- Against stored Massive data on the three 2024 trading days:
  - every regular-session minute is present in both;
  - close prices match exactly on 99.8–100% of bars;
  - 1-minute return correlation is 1.0;
  - volume matches exactly on 99.0–99.4% of bars.

**What differs between Alpaca SIP and Massive:**
- **Volume.** SIP total volume was 0.2–5.0% higher. The difference sits in
  opening/closing-auction bars, block-like round-lot prints and isolated
  large prints. The two providers evidently apply different trade-condition
  rules, so their volumes are **not interchangeable**.
- **Highs/lows.** A few single bars differ in high or low (e.g. one
  regular-session high differed by $0.50 with identical volume). This
  matters for high/low-based features (`max_up`/`max_down`).
- **VWAP.** Massive's minute `vw` lies outside its own bar's [low, high] in
  14–20% of bars (119/836, 127/907, 95/473); Alpaca SIP's never did. The two fields are
  different quantities.
- **Not independent.** Alpaca and Massive are separate vendors, but both
  aggregate the consolidated tape. Their near-identical bars are therefore
  *not* statistically independent evidence; agreement checks
  implementation, not the underlying market record.

### Architecture

Market bars go through a provider-independent contract
(`src/data/fetch/market_provider.py`): each provider supplies only its
transport, credentials, storage identity and an explicit
`ProviderCapabilities` declaration. Chunking, manifest resume, NYSE-session
validation before finalization, provisional windows, checksums and
sibling reconciliation run in one shared code path for every provider.
Downstream code (normalization, `validate_data.py`, market response,
comparison) branches on declared capabilities (`feed_scope`,
`bar_density`, ...), not on provider names.

| Provider | Role now | Feed / scope | History | Status |
|---|---|---|---|---|
| Alpaca `sip` | **canonical** market dataset (default `market.provider`, `feed: "sip"`) | all US exchanges, dense | "since 2016" (2016-03-15 live-verified); Basic plan requires `end` ≥ 15 min old (capped automatically) | live-verified on the free Basic account |
| Alpaca `iex` | sparse single-venue sanity check | IEX only (~2.5% of US volume per Alpaca docs), **sparse** | "since 2016" | live-verified (2024-10-02) |
| Massive | validation/reference only | consolidated, dense | plan-dependent (not declared) | implemented; 1-minute range retrieval not live-verified with the current plan-limited key; existing stored 2024 data used for overlap checks |
| EODHD | future deep-history 1-minute backfill candidate | — | — | **not implemented, not required**; add a `MarketDataProvider` subclass once an account and its actual entitlements are verified |
| Databento | future, optional: trades/BBO/MBP/MBO for *selected event windows only* | — | — | **not implemented, not part of the bootstrap**; `ProviderCapabilities` has `trades/quotes/bbo/l2/l3` flags reserved for it |

**Dataset identity.** Provider + feed + adjustment policy are part of the
manifest key and storage path. Nothing merges them:

- Massive (unchanged): `data/raw/massive/{SYM}/{tf}/{raw|adjusted}/{year}.parquet`, key `SYM:tf:raw`
- Alpaca: `data/raw/alpaca/{feed}/{SYM}/{tf}/{adjustment}/{year}.parquet`, key `SYM:tf:adjustment:feed`

Normalized bars (`data/interim/...`, same layout) carry `source`, `feed`,
`feed_scope`, `adjustment`, `timeframe`, `retrieval_timestamp_utc`,
`normalized_at_utc`, `raw_artifact_path` and `raw_artifact_checksum`;
`vwap`/`transactions` stay null when a provider does not supply them.
`timestamp_utc` is always tz-aware UTC and always the bar's interval
**start** (both providers document this), and the canonical `MarketBar`
rejects naive or non-UTC timestamps.

**Timestamps.** Canonical bars must be timezone-aware (any offset is
converted to UTC; a naive value is never assumed to be UTC) and on the bar
grid:
- always a whole minute;
- for sub-hour timeframes that divide the hour, on that clock grid;
- for hour-plus timeframes, only whole minutes are enforced (providers
  anchor these differently).

A naive or off-grid timestamp (e.g. `09:30:15` for 1-minute bars) is a
hard validation failure. Normalization refuses such raw data; nothing is
floored or rounded.

**IEX is not consolidated data.** Alpaca's free `iex` feed aggregates only
IEX prints. Its volume and trade counts are a fraction of market volume,
and its OHLC are IEX's prints. Minutes with no IEX trade have no bar
(Alpaca emits no bar without an eligible trade). The provider declares this
(`feed_scope: single_venue`, `bar_density: sparse`). Two consequences:

- *Fetch-time validation:* a sparse feed's sessions below the per-minute
  completeness threshold, or missing their open/close minute, are still
  computed and reported (`is_clean` stays false). They do not block
  finalization. A fully elapsed session with zero bars, and every
  structural check (duplicates, invalid OHLC, non-finite values, ...),
  remain hard failures. The policy is recorded in each validation report
  (`bar_density`).
- *Downstream:* market-response rows record `market_data_source/feed/
  feed_scope/adjustment/provenance_basis`, taken from the stored **rows**.
  The bar loader refuses a file in which any of `source/feed/feed_scope/
  adjustment/timeframe` is missing, null, mixed, or different from the
  requested dataset; config never fills in a missing value. The single
  exception is explicit and Massive-only: interim files written before
  feed provenance existed have no `feed`/`feed_scope` columns at all, but
  every other column matches. They are accepted and labelled
  `provenance_basis = legacy_massive_unlabeled`. A response measured on IEX
  bars is a different measurement and must not be pooled with consolidated
  responses unknowingly.

**Benchmark.** The research benchmark remains **2016-01-01 → present**.
It is acquired from Alpaca SIP in controlled phases: calendar year 2016
first, reviewed before the remaining years. EODHD remains only a fallback
candidate and is not needed while Alpaca SIP keeps validating.

**No silent fallback.** If a requested provider fails (credentials,
entitlement, malformed response, validation), that provider is reported
as failed. The run never substitutes another provider's data.

## Canonical dataset version and known exceptions

### Dataset descriptor

The canonical market dataset is frozen and versioned by a committed
descriptor:
`metadata/datasets/core_market_alpaca_sip_1min_raw_qqq_spy.json`. The
parquet data it describes stays local and gitignored.

```bash
python scripts/build_dataset_descriptor.py           # (re)write the descriptor
python scripts/build_dataset_descriptor.py --check   # does local data still match it? (exit 1 if not)
```

It records:
- source (Alpaca / SIP / 1min / raw) and the canonical source policy;
- the benchmark (requested start 2016-01-01, evaluation end 2026-10-02);
- per symbol: first and last timestamp, rows, expected and represented NYSE
  sessions, and provisional windows;
- per year:
  - rows;
  - a data **content SHA-256**;
  - regular-session expected, observed, known-gap, excluded and unexplained
    minutes;
  - a validation label;
  - repository-relative raw and normalized artifact paths with file SHA-256s;
- the known-exception registry version and content hash;
- the manifest's operational state (informational only).

**The fingerprint** (`fingerprint`, `sha256:…`) covers exactly the
scientific fields:
- source and source policy;
- benchmark;
- registry version and content hash;
- per symbol/year: rows, content hash, sessions, coverage minutes, and
  provisional windows.

It deliberately excludes:
- `created_at_utc`;
- artifact paths and machine locations;
- file-byte hashes, because normalized files embed the wall-clock
  `normalized_at_utc`;
- manifest operational statuses;
- JSON formatting.

The content hash covers timestamps, OHLCV, VWAP, transactions and the
provenance columns, in fixed dtypes, sorted by time. Re-normalizing or
cloning the repository therefore keeps the fingerprint. Changing any bar,
row count, provider/feed/adjustment/timeframe, policy or registry entry
changes it. See `src/data/dataset_descriptor.py`.

The latest window (2026-10-01..02) is still `provisional`, within the
provider revision horizon. The descriptor lists it explicitly. It is
finalized by a normal later fetch, never by editing.

### Known-exception registry

`config/market_exceptions.yaml` (loaded by `src/data/market_exceptions.py`)
classifies intervals in which canonical bars are absent. Entries have:
- a session `date`;
- `[start, end)` bar-start intervals, given in both America/New_York local
  time and UTC (the loader verifies they agree);
- symbols, provider and feed scope;
- classification, evidence status (`observed` / `reobserved` /
  `authoritative` / `retired`), evidence, source and notes;
- `retry_appropriate` and `invalidates_event_window`.

The classifications are not interchangeable:

| Classification | Meaning | Validation | Event window |
|---|---|---|---|
| `exchange_closed` | no session (beyond the NYSE calendar) | minutes not expected | tagged `exchange_closed_in_window` |
| `market_wide_halt` | e.g. the March 2020 circuit-breaker halts | minutes not expected; **not** data corruption | kept, tagged `market_halt_in_window` |
| `legitimate_no_trade_interval` | no qualifying trades for this symbol/feed | minutes not expected | tagged `no_trade_interval_in_window` |
| `provider_gap` | trading happened, vendor archive has no bars | still missing and counted; never filled | excluded/null, tagged `provider_gap_in_window` |
| `temporary_fetch_failure` | operational failure | still a failure; always retryable | — |

The registry is seeded from the 2016–2026 backfill gap inventory:
- 10 Alpaca SIP `provider_gap` intervals:
  - 2016-02-02 (QQQ and SPY);
  - 2016-02-22 (QQQ);
  - 2018-05-02/03 (QQQ);
  - 2019-08-12 (SPY);
  - 2021-05-05 and 2023-06-05 (SPY; sub-threshold holes the 98% rule alone
    would hide).
- 4 `market_wide_halt` intervals (2020-03-09, 12, 16 and 18). These are the
  observed no-bar intervals, consistent with the reported Level-1 halts.
  Their status stays `observed` until an authoritative halt record is
  attached.

Any change bumps `registry_version`, which changes the dataset fingerprint.

**Operational vs. scientific state.** A chunk whose only missing minutes are
covered by registered, non-retryable `provider_gap` entries is finalized as
`complete_with_known_gaps` (manifest `known_exception_ids` lists them):
- **operationally resolved:** it is skipped on reruns, counts toward the
  watermark, and `update_data.py` stops refetching the immutable vendor hole;
- **scientifically incomplete:** validation still counts those minutes as
  `known_gap_minutes`, never reports the window as clean, and never fills
  it.

Any *unexplained* missing minute still fails the chunk. A gap with
`retry_appropriate: true`, or any temporary fetch failure, stays `failed`
and is retried. If a registered no-bar interval turns out to contain bars,
validation reports the entry as stale. Chunks that failed before the
registry existed (2016-02, 2018-05, 2019-08 and 2020-03) move to
`complete_with_known_gaps` (or `complete`, for the halt months) on the next
normal update. That one re-fetch is also the confirming re-observation.

**Event-window query.** Downstream code asks:
```python
from src.data.market_exceptions import load_registry_for_config
report = load_registry_for_config(config).query_window("QQQ", start, end)   # tz-aware, [start, end)
report.has_provider_gap, report.has_market_halt, report.invalidates_event_window, report.tags, report.hits
```

Intended event-window policy, for the macro market-response layer (not yet
wired in):
- **A provider gap intersects a required window:** that feature/window is
  excluded or null, tagged with the data-quality reason, and never
  interpolated.
- **A market halt intersects the window:** the event is kept. Returns
  spanning a halt are real, but the row is tagged `market_halt_in_window`,
  because a nominal 5m/15m horizon then covers more clock time than traded
  time.
- **A missing optional extended-hours minute:** the existing density and
  window-sufficiency rules apply; nothing is fabricated.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in MASSIVE_API_KEY / FRED_API_KEY (+ APCA_API_KEY_ID / APCA_API_SECRET_KEY for Alpaca)
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
nothing. Market providers are ordinary sources: `--sources massive` works
as before, `--sources alpaca` or `--sources massive,alpaca` select others;
without `--sources`, the providers in config `market.providers` are used
(default `["massive"]`). `--symbols` applies to every market provider. `validate_data.py` exits non-zero on a **hard** data-integrity
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

### Free market-data smoke test and cross-provider validation

Requires only free Alpaca Basic-plan keys (`APCA_API_KEY_ID`,
`APCA_API_SECRET_KEY`):

```bash
# 1. a few weeks of Alpaca 1-minute bars (feed comes from config providers.alpaca.feed: "sip" by default;
#    set "iex" to collect the single-venue sanity-check dataset instead -- each feed is stored separately)
python scripts/fetch_historical_data.py --sources alpaca --symbols QQQ --start 2024-01-02 --end 2024-01-31

# 2. the same window from Massive, if your plan allows it
python scripts/fetch_historical_data.py --sources massive --symbols QQQ --start 2024-01-02 --end 2024-01-31

# 3. descriptive discrepancy report (reads stored data only; fetches nothing)
python scripts/compare_market_providers.py --providers massive,alpaca --symbols QQQ --start 2024-01-02 --end 2024-01-31
```

The comparison (`src/data/validation/cross_provider.py`) aligns bars on
exact bar-start timestamps. It reports:

- coverage: in both, only in A, only in B, overlap ratios, and
  regular-session vs. extended-hours splits based on real NYSE sessions;
- per-session presence differences;
- OHLC absolute and relative differences (median/p95/max);
- volume differences and ratios;
- the correlation of close-to-close returns over consecutive minutes
  present in both providers;
- the largest close discrepancies;
- each provider's manifest gaps in the window, so a coverage difference
  can be traced to a failed or missing fetch.

These are **descriptive metrics, not pass/fail tolerances**. Return
correlation counts a return as regular-session only when both of its
bars are inside the regular session. Hard integrity failures (naive or
duplicate timestamps, invalid OHLC, non-finite values, misaligned bars)
are reported separately and make the command exit 1. So does a side with
no usable data: the output says whether the stored file is absent (`NO
STORED FILE`) or exists without a bar in the requested window (`NO BARS
IN WINDOW`). This is an inability to compare, not a discrepancy. Pairs with different timeframes or adjustment policies
are refused (exit 2). When feed scopes differ, the report flags volume as
not comparable as full-market volume. Reports are written to
`data/reports/provider_comparison_<a>_vs_<b>_<SYM>_<timeframe>_<adjustment>_<start>_<end>.json`.

`compare_market_providers.py` compares the Alpaca feed currently set in
`providers.alpaca.feed`; it has no `--feed` option yet.

Market response uses the default provider (Alpaca SIP) and writes
`macro_market_response_alpaca-sip_raw*.{parquet,csv}`. Pass
`--market-provider massive` (or the `--massive-dir` alias) for the
Massive-based output, which keeps its original unsuffixed names.

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
  month and the trailing `revision_overlap_days` of every market provider's data are
  never checkpointed "complete" — they're re-fetched on every run to
  catch late prints, corrections, and revisions.
- **Validated before finalization, not just after sorting/dedup**: a
  market-provider chunk (Massive, Alpaca, ...) is only checkpointed `complete` if it passes
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
  matches. If a shared market year-file is missing or corrupt, *every*
  checkpoint backed by it is invalidated up front — not just the one
  chunk being re-fetched — and after a rewrite, each sibling checkpoint
  is individually re-verified against the rebuilt file's actual rows
  before its checksum is refreshed; one that no longer has its claimed
  data is invalidated instead of silently re-trusted. "Still has its
  claimed data" means the sibling's exact date range passes the **same
  validation a fresh fetch is finalized with**:
  - the provider's bar density and the configured timeframe apply;
  - it is evaluated as of the entry's own acquisition time, so a
    provisional window is not held to bars that were not yet due;
  - a single surviving row is not enough;
  - an `empty` entry stays valid only where the range has no NYSE session
    and no rows.

  This applies to `provisional` siblings too. Around a month boundary, two
  provisional chunks (e.g. Sept and Oct on Oct 1–3) share one year file,
  and rewriting one used to leave the other's checksum stale. A
  provisional entry whose artifact is missing or altered is invalidated up
  front, like a complete one.
- **Durability**: every market provider writes/checkpoints each month immediately
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

- **Alpaca adapter is live-verified** (2026-10-04, free Basic account), in addition to the fixture tests of the documented contract (`/v2/stocks/bars` reference, market-data FAQ, subscription plans; checked 2026-10-03). Confirmed live:
  - field mapping (`t/o/h/l/c/v/n/vw`) and UTC `Z` timestamps at bar start, on the minute grid;
  - New York day bounds and extended hours;
  - multi-page `next_page_token` pagination;
  - empty `bars` object on a holiday;
  - historical SIP on Basic for 2016–2024.

  Live responses carry no `currency` key, which the documentation shows; it is unused. Not yet seen live: an error response, and the recent-SIP 15-minute rejection. Alpaca's `asof` symbol-rename mapping is left at the provider default: META queries return FB history, unlike Massive's per-ticker behavior. Every manifest entry records `symbol_mapping: alpaca_default_asof_not_sent`, and a test pins this behavior. This is irrelevant for QQQ/SPY. Before adding renamed tickers, decide a security-identity policy (ticker-as-of-date vs. continuous security) and verify Alpaca's documented `asof` semantics; this is not guessed now.
- **Credential redaction is a last line of defense** (`src/data/redaction.py`). It removes:
  - the exact value of every credential env var the process holds;
  - credential-shaped text: query/form parameters including URL-encoded ones, header lines, JSON and Python-repr pairs, Authorization/Bearer/Basic values.

  It is applied at every boundary: fetch-layer log calls, every `Manifest.record` error string (all providers), CLI summaries, and a redacting filter on the CLI log handlers. Bare words containing "key" and ordinary numeric values are not touched.
- **HTTP error messages are credential-redacted**: `requests` puts the full request URL into `HTTPError`/`ConnectionError` text, and Massive authenticates via `apiKey=` in the query string. Before this change, a real key could therefore reach log lines and manifest `error` fields. `http_utils.request_with_retry` now redacts query credentials and keeps the provider's error body instead. **Manifests written before this change may still contain a key in old `error` strings.** `data/manifests/` is gitignored, but rotate the key if that file was ever shared.
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
