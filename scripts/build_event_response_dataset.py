#!/usr/bin/env python3
"""Build the macro event -> surprise -> market response research dataset (local files only).

    python scripts/build_event_response_dataset.py

Inputs (nothing is fetched): normalized Forex Factory events, the official-value reconciliation
(FRED/ALFRED where available), the frozen canonical market dataset (verified against its
committed descriptor fingerprint), the known-exception registry, config/event_research.yaml.

Outputs:
  data/processed/event_response/event_response_v<feature_version>.parquet
  data/processed/event_response/event_response_windows_v<feature_version>.parquet
  metadata/research/event_response_v<feature_version>.json   (provenance + quality summary; small)
  data/reports/event_response/v<feature_version>/quality_*.csv, diagnostics_*.csv (versioned;
      the inventory with per-file SHA-256 is recorded in the metadata)

EXIT CODE: 0 on success; 1 if the local market data no longer matches the committed descriptor.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.data.config import load_config  # noqa: E402
from src.data.dataset_descriptor import build_descriptor, verify_against  # noqa: E402
from src.data.fetch.market_provider import get_market_provider  # noqa: E402
from src.data.market_exceptions import load_registry_for_config  # noqa: E402
from src.data.validation.macro_release_pipeline import run_macro_validation  # noqa: E402
from src.research.baseline import baseline_id  # noqa: E402
from src.research.dataset import (  # noqa: E402
    SCHEMA_NOTE, WIDE_KEY, WINDOW_KEY, build_event_response, canonical_content_sha256, implementation_fingerprint,
    load_canonical_bars, research_input_identity,
)
from src.research.diagnostics import (  # noqa: E402
    baseline_evaluation, diagnostics_tables, quality_tables, window_status_counts,
)
from src.research.events import PROVENANCE_STATEMENT, load_ff_events, load_research_spec, official_validation_summary  # noqa: E402

DESCRIPTOR = REPO_ROOT / "metadata" / "datasets" / "core_market_alpaca_sip_1min_raw_qqq_spy.json"


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--symbols", default="QQQ,SPY")
    args = p.parse_args(argv)
    config = load_config()
    spec = load_research_spec()
    registry = load_registry_for_config(config)
    provider = get_market_provider(config.primary_market_provider, config)
    caps = provider.capabilities()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    committed = json.loads(DESCRIPTOR.read_text(encoding="utf-8"))
    current = build_descriptor(config, provider, registry, committed["dataset_name"], symbols,
                               dt.date.fromisoformat(committed["benchmark"]["requested_start"]),
                               dt.date.fromisoformat(committed["benchmark"]["evaluation_end"]))
    problems = verify_against(committed, current)
    if problems:
        print("ERROR: local market data does not match the committed dataset descriptor:", *problems, sep="\n  ")
        return 1
    fingerprint = committed["fingerprint"]

    ff = load_ff_events(config)
    research_end = dt.date.fromisoformat(spec["split"]["research_end"])
    rec, _, input_counts = run_macro_validation(dt.date(2016, 1, 1), research_end, config=config)
    bars = load_canonical_bars(config, provider, symbols, dt.date(2016, 1, 1), research_end)
    identity = {"market_provider": caps.provider, "market_feed": caps.feed, "market_feed_scope": caps.feed_scope,
                "market_adjustment": caps.adjustment, "market_timeframe": config.market_timeframe,
                "market_dataset_fingerprint": fingerprint}
    wide, windows, events = build_event_response(ff, spec, registry, bars, identity, official_rows=rec.rows,
                                                 provider=caps.provider, feed=caps.feed)

    v = spec["response_feature_version"]
    out_dir = config.processed_root / "event_response"
    out_dir.mkdir(parents=True, exist_ok=True)
    wide_path, win_path = out_dir / f"event_response_v{v}.parquet", out_dir / f"event_response_windows_v{v}.parquet"
    hashes = {}
    for name, df, path, key in (("event_response", wide, wide_path, WIDE_KEY),
                                ("event_response_windows", windows, win_path, WINDOW_KEY)):
        df.to_parquet(path, index=False)
        before, after = canonical_content_sha256(df, key), canonical_content_sha256(pd.read_parquet(path), key)
        if before != after:
            print(f"ERROR: {name} content hash changed across parquet write/read ({before} != {after})")
            return 1
        hashes[name] = {"path": config.relative_to_repo(path), "rows": int(len(df)), "content_sha256": before,
                        "hash_key": key, "roundtrip_verified": True}

    # Versioned report set: only this response_feature_version's reports live in this directory.
    report_dir = config.interim_root.parent / "reports" / "event_response" / f"v{v}"
    report_dir.mkdir(parents=True, exist_ok=True)
    quality = quality_tables(wide, events, windows)
    diag = diagnostics_tables(wide, spec)
    diag["baseline_evaluation"] = baseline_evaluation(wide, spec)
    report_frames = {f"{prefix}_{name}.csv": df for prefix, tables in (("quality", quality), ("diagnostics", diag))
                     for name, df in tables.items()}
    # Remove ONLY stale files this builder owns (its quality_/diagnostics_ CSV naming) that are not
    # part of the current set; nothing else in the data directories is touched.
    stale = sorted(p.name for p in report_dir.glob("*.csv")
                   if p.name.startswith(("quality_", "diagnostics_")) and p.name not in report_frames)
    for name in stale:
        (report_dir / name).unlink()
    report_inventory = {}
    for name, df in sorted(report_frames.items()):
        path = report_dir / name
        df.to_csv(path, index=False)
        report_inventory[name] = {"rows": int(len(df)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    mapping_raw = yaml.safe_load((REPO_ROOT / "config" / "event_mapping.yaml").read_text(encoding="utf-8"))
    excluded_windows = windows[windows["status"] == "event_excluded"]
    weak = {f: fs["surprise_measure_quality"] for f, fs in spec["families"].items() if fs.get("surprise_measure_quality")}
    meta = {
        "dataset": f"event_response_v{v}",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "implementation": implementation_fingerprint(REPO_ROOT),
        "inputs": {
            "market_dataset": {"name": committed["dataset_name"], "fingerprint": fingerprint,
                               "descriptor": config.relative_to_repo(DESCRIPTOR), "verified_against_local_data": True},
            "known_exception_registry": {"path": registry.source_path, "registry_version": registry.registry_version,
                                         "content_sha256": registry.content_sha256()},
            "research_inputs": research_input_identity(ff, rec.rows, mapping_raw, spec),
            "reconciliation_input_counts": input_counts,
            "research_spec": {"path": "config/event_research.yaml", "spec_version": spec["spec_version"],
                              "surprise_spec_version": spec["surprise_spec_version"], "response_feature_version": v,
                              "baseline_model": baseline_id(spec), "split_version": spec["split"]["version"]},
        },
        "outputs": hashes,
        "reports": {"directory": config.relative_to_repo(report_dir), "inventory": report_inventory,
                    "stale_owned_files_removed_this_build": stale,
                    "note": "only the files listed in the inventory belong to this artifact version"},
        "point_in_time_provenance": {
            "statement": PROVENANCE_STATEMENT,
            "value_provenance": "retrospective_historical_page",
            "forecast_point_in_time_status": "historical_page_unverified_point_in_time (all events)",
            "official_validation": official_validation_summary(events),
            "mql5_policy": "MQL5 timestamps quarantined; MQL5 values never upgrade Forex Factory or official verification",
        },
        "surprise_measure_quality": weak,
        "fomc_note": ("FED_FUNDS_RATE surprise = decision - Forex Factory forecast: 2 non-zero values 2016-2026; a "
                      "weak_surprise_proxy. FOMC market-response rows are kept, but surprise-response inference "
                      "needs a pre-decision market-implied expectation (fed funds futures, OIS), which is not available."),
        "baseline": {"model": baseline_id(spec), "evaluation_paradigm": spec["baseline"]["evaluation_paradigm"],
                     "description": ("walk-forward / online expanding: each expectation uses all strictly earlier usable "
                                     "releases of the same symbol/family/surprise-sign group, including earlier "
                                     "validation/test releases; not a frozen development model; per-row training "
                                     "cutoff and history count are stored"),
                     "rule_changes_after_test_observation": "none"},
        "split": {**{k: spec["split"][k] for k in ("development", "validation", "test", "research_end")},
                  "rationale": ("chosen on calendar and per-family sample-size considerations after inspecting event "
                                "counts, before computing response statistics; not pre-registered; descriptive use")},
        "counts": {
            "event_rows": int(len(wide)), "events": int(events["event_id"].nunique()),
            "events_by_status": {k: int(n) for k, n in events["event_status"].value_counts().sort_index().items()},
            "releases_with_time": int(events["release_id"].nunique()),
            "usable_releases": int(events.loc[events["event_status"] == "usable", "release_id"].nunique()),
            "window_rows": int(len(windows)), "window_status": window_status_counts(windows),
            "event_excluded_windows": int(len(excluded_windows)),
            "usable_post5m_event_rows": int(wide["usable_post_response"].sum()),
        },
        "schema": SCHEMA_NOTE,
        "quality_summary": quality["by_family_symbol"].to_dict(orient="records"),
    }
    meta_path = REPO_ROOT / "metadata" / "research" / f"event_response_v{v}.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"event_response: {len(wide)} rows ({events['event_id'].nunique()} events x {len(symbols)} symbols); "
          f"windows: {len(windows)} rows {window_status_counts(windows)}; market fingerprint {fingerprint}")
    print(f"wrote {wide_path}\nwrote {win_path}\nwrote {meta_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
