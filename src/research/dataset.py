"""Assemble the event-response research dataset (event x symbol) and its provenance.

Two tables:
  event_response          one row per (macro event, symbol)
  event_response_windows  one row per (release, symbol, window): every window metric, coverage,
                          registry hits, status and reason
STATISTICAL UNIT: members of one simultaneous release share one market response. `release_id`
is the observational cluster (with symbol); see src/research/filters.py.

EXCLUSION PROPAGATION: a release whose member events are all excluded yields only
`event_excluded` window rows (features null, `window_reason` = the event reasons); an excluded
event's own event_response row never carries a response, even when other members of its release
are usable.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..data.config import AppConfig
from ..data.fetch.market_provider import MarketDataProvider
from ..data.market_exceptions import MarketExceptionRegistry
from ..features.market_response import check_market_provenance
from .baseline import add_baseline, assign_split
from .event_response import (
    EVENT_EXCLUDED, METRICS, SymbolBars, calendar_frame, compute_release_windows, window_specs,
)
from .events import USABLE, build_event_table
from .filters import usable_response
from .surprise import SURPRISE_COLUMNS, add_surprises

WIDE_KEY = ["event_id", "symbol"]
WINDOW_KEY = ["release_id", "symbol", "window"]


def load_canonical_bars(config: AppConfig, provider: MarketDataProvider, symbols: Iterable[str],
                        start: dt.date, end: dt.date) -> Dict[str, SymbolBars]:
    caps = provider.capabilities()
    expected = {"source": caps.source_label, "feed": caps.feed, "feed_scope": caps.feed_scope,
                "adjustment": caps.adjustment, "timeframe": config.market_timeframe}
    out = {}
    for sym in symbols:
        frames = []
        for y in range(start.year, end.year + 1):
            p = provider.interim_year_path(sym, config.market_timeframe, y)
            if p.exists():
                frames.append(pd.read_parquet(p))
        if not frames:
            raise FileNotFoundError(f"no canonical bars for {sym}")
        df = pd.concat(frames, ignore_index=True)
        check_market_provenance(df, sym, provider.interim_dataset_root(), expected)  # strict row provenance
        out[sym] = SymbolBars(df)
    return out


def _propagate_exclusions(windows: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    windows = windows.copy()
    windows["window_reason"] = None
    windows["release_excluded_member_events"] = 0
    timed = events[events["release_id"].notna()]
    by_release = {rid: g for rid, g in timed.groupby("release_id")}
    metric_cols = [c for c in METRICS + ["volume_rel_status", "volume_rel_n_windows"] if c in windows.columns]
    for i, rid in windows["release_id"].items():
        g = by_release.get(rid)
        if g is None:
            continue
        excluded = g[g["event_status"] != USABLE]
        windows.at[i, "release_excluded_member_events"] = int(len(excluded))
        if len(excluded) == len(g):  # no usable member: the whole release is excluded
            windows.at[i, "status"] = EVENT_EXCLUDED
            windows.at[i, "window_reason"] = "; ".join(f"{r.event_id}: {r.event_status} ({r.event_status_reason})"
                                                       for r in excluded.itertuples())
            for c in metric_cols:
                windows.at[i, c] = None
    return windows


def build_event_response(
    ff_events: pd.DataFrame,
    spec: Dict[str, Any],
    registry: MarketExceptionRegistry,
    bars: Dict[str, SymbolBars],
    market_identity: Dict[str, Any],
    official_rows: Optional[List[dict]] = None,
    provider: str = "alpaca",
    feed: str = "sip",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    research_end = dt.date.fromisoformat(spec["split"]["research_end"])
    events = add_surprises(build_event_table(ff_events, spec, official_rows), spec)
    ts = events["release_timestamp_utc"].dropna()
    sched = calendar_frame(ts.min().date() - dt.timedelta(days=150), research_end + dt.timedelta(days=10))

    win_rows = []
    releases = sorted(set(events["release_id"].dropna()))
    for sym, sb in bars.items():
        for rid in releases:
            win_rows.extend(compute_release_windows(pd.Timestamp(rid), sym, sb, registry, spec, sched, research_end,
                                                    provider=provider, feed=feed))
    windows = _propagate_exclusions(pd.DataFrame(win_rows), events)

    names = [w for w, _, _ in window_specs(spec)]
    piv = {}
    for (sym, rid), g in windows.groupby(["symbol", "release_id"]):
        g = g.set_index("window")
        rec = {f"{w}_ret": g.at[w, "ret"] for w in names}
        rec.update({f"{w}_status": g.at[w, "status"] for w in names})
        rec["release_session_state"] = g["release_session_state"].iloc[0]
        rec["release_timestamp_minute_aligned"] = bool(g["release_timestamp_minute_aligned"].iloc[0])
        ids = sorted({i for v in g["exception_ids"].dropna() for i in v.split(",")})
        rec["known_exception_ids"] = ",".join(ids) or None
        rec["any_provider_gap"] = bool(g["has_provider_gap"].any())
        rec["any_market_halt"] = bool(g["has_market_halt"].any())
        post, pre = g[g["side"] == "post"], g[g["side"] == "pre"]
        rec["post_windows_insufficient"] = int((post["status"] == "insufficient_market_window").sum())
        rec["pre_windows_insufficient"] = int((pre["status"] == "insufficient_market_window").sum())
        rec["provisional_market_data"] = bool((g["status"] == "provisional_market_data").any())
        rec["post60m_coverage"] = g.at["post60m", "coverage"] if "post60m" in g.index else None
        rec["pre60m_coverage"] = g.at["pre60m", "coverage"] if "pre60m" in g.index else None
        rec["release_volume"] = g.at["post1m", "volume"] if "post1m" in g.index else None
        rec["release_volume_rel"] = g.at["post1m", "volume_rel"] if "post1m" in g.index else None
        v5 = g.at["post5m", "volume"] if "post5m" in g.index else None
        v60 = g.at["post60m", "volume"] if "post60m" in g.index else None
        rec["post_volume_concentration_5_60"] = (v5 / v60) if v5 is not None and v60 and not pd.isna(v5) and not pd.isna(v60) else None
        piv[(sym, rid)] = rec

    rows = []
    for _, e in events.iterrows():
        for sym in bars:
            rec = {**e.to_dict(), "symbol": sym, **market_identity}
            w = dict(piv.get((sym, e["release_id"])) or {f"{n}_status": "no_release_time" for n in names})
            if e["event_status"] != USABLE:  # an excluded event never carries a response
                for n in names:
                    w[f"{n}_ret"] = None
                    w[f"{n}_status"] = EVENT_EXCLUDED
                for c in ("release_volume", "release_volume_rel", "post_volume_concentration_5_60",
                          "post60m_coverage", "pre60m_coverage"):
                    w[c] = None
            rec.update(w)
            rows.append(rec)
    wide = pd.DataFrame(rows)
    wide["split"] = [assign_split(d, spec) for d in wide["release_date_et"]]
    wide["usable_post_response"] = usable_response(wide, "post5m")
    wide = add_baseline(wide, spec)
    wide = canonical_dtypes(wide).sort_values(WIDE_KEY, kind="mergesort").reset_index(drop=True)
    windows = canonical_dtypes(windows).sort_values(WINDOW_KEY, kind="mergesort").reset_index(drop=True)
    return wide, windows, events


# --------------------------------------------------------------------------- canonical content identity
def canonical_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Give mixed object columns one stable dtype before writing (numbers -> float64, booleans ->
    nullable boolean), so the stored parquet has no serialization-dependent typing."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype != object:
            continue
        vals = [v for v in out[c] if not _is_null(v)]
        if vals and all(isinstance(v, (bool, np.bool_)) for v in vals):
            out[c] = pd.array([None if _is_null(v) else bool(v) for v in out[c]], dtype="boolean")
        elif vals and all(isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_)) for v in vals):
            out[c] = pd.to_numeric(out[c], errors="raise").astype("float64")
    return out


def _is_null(v) -> bool:
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    try:
        return bool(pd.isna(v)) if not isinstance(v, (list, tuple, np.ndarray)) else False
    except (TypeError, ValueError):
        return False


def _canonical_value(v):
    if _is_null(v):
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if f.is_integer() and abs(f) < 2 ** 53:
            return int(f)
        return format(f, ".17g")  # exact float64 round-trip representation
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        t = pd.Timestamp(v)
        return t.tz_convert("UTC").isoformat() if t.tzinfo is not None else "naive:" + t.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_canonical_value(x) for x in v]
    return str(v)


def canonical_content_sha256(df: pd.DataFrame, key: Sequence[str]) -> str:
    """Scientific content hash independent of serialization: columns in sorted order, rows sorted
    by `key`, values canonicalized (nulls unified, integral floats as ints, other floats at full
    round-trip precision, timestamps as UTC ISO strings). hash(df) == hash(read(write(df)))."""
    cols = sorted(df.columns)
    recs = [[_canonical_value(v) for v in row] for row in df[cols].itertuples(index=False, name=None)]
    order = sorted(range(len(recs)), key=lambda i: json.dumps([recs[i][cols.index(k)] for k in key], default=str))
    h = hashlib.sha256(json.dumps(cols).encode())
    for i in order:
        h.update(b"\n" + json.dumps(recs[i], separators=(",", ":"), default=str).encode())
    return h.hexdigest()


def spec_sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


MACRO_INPUT_COLUMNS = [
    "event_id", "source_event_id", "event_family", "indicator", "release_bundle", "reference_period",
    "release_timestamp_utc", "timestamp_quality", "actual", "actual_unit", "provider_forecast",
    "provider_forecast_unit", "previous", "previous_unit", "revised_previous", "source", "raw_artifact_checksum",
]
OFFICIAL_INPUT_FIELDS = [
    "ff_event_id", "event_family", "official_source", "official_series_id", "official_reference_period",
    "official_vintage_date", "official_release_vintage_value", "official_unit", "official_latest_value",
    "official_latest_vintage_date",
]


def research_input_identity(ff: pd.DataFrame, official_rows: List[dict], event_mapping_raw: Any,
                            spec: Dict[str, Any]) -> Dict[str, Any]:
    """Every input able to change scientific output: calendar fields that drive inclusion,
    grouping and values; the official numbers used for validation; the event mapping; the spec.
    Wall-clock normalization timestamps are deliberately excluded."""
    macro = ff[[c for c in MACRO_INPUT_COLUMNS if c in ff.columns]]
    official = pd.DataFrame([{k: r.get(k) for k in OFFICIAL_INPUT_FIELDS} for r in official_rows if r.get("ff_event_id")],
                            columns=OFFICIAL_INPUT_FIELDS)
    parts = {
        "macro_events_sha256": canonical_content_sha256(macro, ["event_id"]),
        "official_reconciliation_sha256": canonical_content_sha256(official, ["ff_event_id"]),
        "event_mapping_sha256": spec_sha256(event_mapping_raw),
        "research_spec_sha256": spec_sha256(spec),
    }
    parts["combined_sha256"] = spec_sha256(parts)
    return parts


def implementation_fingerprint(repo_root: Path, paths: Sequence[str] = ("src", "scripts", "config")) -> Dict[str, Any]:
    """HEAD + dirty flag + a hash of the uncommitted scientific code/config (tracked diff against
    HEAD plus untracked files) so an artifact built from a dirty tree is traceable to the exact
    implementation. When clean, the diff hash is that of an empty change set."""
    def run(*a, binary=False):
        r = subprocess.run(["git", *a], cwd=repo_root, capture_output=True)
        return r.stdout if binary else r.stdout.decode().strip()
    head = run("rev-parse", "HEAD")
    diff = run("diff", "HEAD", "--binary", "--", *paths, binary=True)
    untracked = sorted(run("ls-files", "--others", "--exclude-standard", "--", *paths).splitlines())
    h = hashlib.sha256(diff)
    for f in untracked:
        h.update(b"\0" + f.encode() + b"\0" + (repo_root / f).read_bytes())
    dirty = bool(run("status", "--porcelain"))
    return {"head_commit": head, "working_tree_dirty": dirty, "scientific_paths": list(paths),
            "uncommitted_changes_sha256": h.hexdigest(), "untracked_scientific_files": untracked,
            "note": ("HEAD alone does NOT reproduce this artifact: it was built from uncommitted changes "
                     "identified by uncommitted_changes_sha256") if dirty else "built from a clean tree at HEAD"}


SCHEMA_NOTE = {
    "grain": "event_response: event x symbol; event_response_windows: release_id x symbol x window",
    "statistical_unit": ("market-response observational cluster = (release_id, symbol); simultaneous release members "
                         "(e.g. CPI m/m, CPI y/y, Core CPI m/m, Core CPI y/y) share one response and must be clustered "
                         "or aggregated by release_id in any cross-family analysis, never counted as independent draws"),
    "window_metrics": METRICS,
    "ex_post_response_characterization": ["mfe", "mae"],
    "ex_post_note": ("mfe/mae depend on the realized post-release direction and path; like every response metric "
                     "they are outcomes, never predictors available before the release"),
    "surprise_columns": SURPRISE_COLUMNS,
    "release_timing": ("release_time_precision=scheduled_minute (Forex Factory); release_bar_timing_status="
                       "minute_precision means the release bar may contain seconds of pre-publication trading; "
                       "straddling_release_bar would mark a timestamp with seconds (none in current data)"),
}
