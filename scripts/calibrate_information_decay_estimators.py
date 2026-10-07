#!/usr/bin/env python3
"""Synthetic MI-estimator calibration for M3 information decay v1 (SYNTHETIC DATA ONLY).

    python scripts/calibrate_information_decay_estimators.py --benchmark          # timing only
    python scripts/calibrate_information_decay_estimators.py --profile dev
    python scripts/calibrate_information_decay_estimators.py --profile final --workers 10

Reads ONLY config/information_decay_v1.yaml and the frozen methodology's git identity. It never
reads real surprise / response / residual values: the run executes inside
isolation.real_data_guard() (blocks data/processed, data/interim, data/raw and any .parquet) and the
calibration package depends on numpy only.

Outputs (profile `final`):
  data/reports/information_decay/calibration_v1/*.csv, calibration_summary.json
  metadata/research/information_decay_calibration_v1.json   (provenance: config hash,
      methodology freeze commit, code commit / dirty state, output hashes, runtime)
Other profiles write to data/reports/information_decay/calibration_v1_<profile>/ and never touch
the metadata file. CSV and summary contents are deterministic (no timestamps / timings inside).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay.decisions import decide  # noqa: E402
from src.research.information_decay.isolation import real_data_guard  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "information_decay_v1.yaml"
METADATA_PATH = REPO_ROOT / "metadata" / "research" / "information_decay_calibration_v1.json"
REPORT_ROOT = REPO_ROOT / "data" / "reports" / "information_decay"

LEAD_COLUMNS = ["cell", "scenario", "mode", "is_null", "n", "estimator", "procedure", "method", "reps", "truth_bits"]


# ---------------------------------------------------------------------------------------------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip()


def implementation_state() -> Dict[str, Any]:
    """Pattern-based scientific implementation fingerprint (Amendment 04 §6): every
    src/research/information_decay/**/*.py, scripts/*information_decay*.py and
    config/information_decay*.yaml file, sorted, per-file SHA-256 plus aggregate."""
    from src.research.information_decay.provenance import implementation_fingerprint
    return implementation_fingerprint(REPO_ROOT)

def methodology_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    m = cfg["methodology"]
    exists = subprocess.run(["git", "cat-file", "-e", f"{m['freeze_commit']}^{{commit}}"], cwd=REPO_ROOT,
                            capture_output=True).returncode == 0
    unchanged = subprocess.run(["git", "diff", "--quiet", m["freeze_commit"], "--", m["path"]],
                               cwd=REPO_ROOT).returncode == 0 if exists else False
    blob = git("rev-parse", f"{m['freeze_commit']}:{m['path']}") if exists else None
    return {"path": m["path"], "freeze_commit": m["freeze_commit"], "freeze_commit_exists": exists,
            "document_unchanged_since_freeze": unchanged, "frozen_blob_sha1": blob}


def fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "nan" if math.isnan(v) else format(v, ".10g")
    return str(v)


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    cols: List[str] = [c for c in LEAD_COLUMNS if any(c in r for r in rows)]
    extra = sorted({k for r in rows for k in r} - set(cols))
    cols += extra
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([fmt(r.get(c)) for c in cols])
    path.write_text(buf.getvalue(), encoding="utf-8")


def split_tables(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    mi = ("gcmi", "ksg", "ilin", "hist")
    inferential = [r for r in rows if r["procedure"] not in ("interval", "outlier_shift", "clean")]
    return {
        "estimator_bias.csv": [r for r in inferential if r["estimator"].startswith(mi) and "mean_obs_bits" in r],
        "estimator_rmse.csv": [r for r in inferential if r["estimator"].startswith(mi) and "rmse_obs_bits" in r],
        "type1_error.csv": [r for r in inferential if r["is_null"] and "adjusted_rate" in r
                            and not r["cell"].startswith(("s8", "s9"))],
        "power.csv": [r for r in inferential if not r["is_null"] and "adjusted_rate" in r
                      and not r["cell"].startswith(("s8", "s9"))],
        "tie_policy.csv": [r for r in inferential if r["cell"].startswith("s9") and "adjusted_rate" in r],
        "residual_null_calibration.csv": [r for r in inferential if r["cell"].startswith("s8") and "adjusted_rate" in r],
        "bootstrap_coverage.csv": [r for r in rows if r["procedure"] == "interval"],
        "outlier_robustness.csv": [r for r in rows if r["procedure"] == "outlier_shift"],
        "ksg_null_balance.csv": [r for r in inferential if r["cell"] == "s1h_null_hetero" and r["estimator"].startswith("ksg")],
    }


def scenario_truth_rows(ctx: cal.Context) -> List[Dict[str, Any]]:
    rows = []
    for cell in ctx.config["cells"]:
        rows.append({"cell": cell["id"], "scenario": cell["scenario"], "mode": cell.get("mode", "A"),
                     "is_null": bool(cell.get("is_null", False)), "truth_bits": cal.cell_truth_bits(ctx, cell),
                     "strength": ctx.strengths.get(cell["scenario"]), "rho": cell.get("rho"),
                     "sizes": ",".join(str(n) for n in cal.cell_sizes(ctx.config, cell))})
    return rows


# ---------------------------------------------------------------------------------------------
def benchmark(cfg: Dict[str, Any], strengths: Dict[str, float]) -> None:
    """TIMING ONLY: seconds per replicate under the `final` in-replicate counts. Estimator outputs
    are discarded unseen."""
    ctx = cal.make_context(cfg, "final", strengths)
    prepared = cal._prepared_cells(ctx)
    c = ctx.counts
    total = 0.0
    print(f"{'cell':26s} {'n':>4s} {'ksg rep s':>10s} {'plain rep s':>11s}")
    with real_data_guard(REPO_ROOT):
        for cell in cfg["cells"]:
            cell_total = 0.0
            for n in cal.cell_sizes(cfg, cell):
                probe = n in (60, 120)
                if probe:
                    t0 = time.perf_counter()
                    cal.run_replicate(ctx, prepared[cell["id"]], n, 0)
                    t_ksg = time.perf_counter() - t0
                    t0 = time.perf_counter()
                    cal.run_replicate(ctx, prepared[cell["id"]], n, c["reps_ksg"] + 1)
                    t_plain = time.perf_counter() - t0
                    print(f"{cell['id']:26s} {n:4d} {t_ksg:10.3f} {t_plain:11.3f}", flush=True)
                    last = (t_ksg, t_plain, n)
                # extrapolate other sizes quadratically from the nearest probe
            for n in cal.cell_sizes(cfg, cell):
                t_ksg, t_plain, n0 = last
                scale = (n / n0) ** 2
                cell_total += scale * (c["reps_ksg"] * t_ksg + (c["reps"] - c["reps_ksg"]) * t_plain)
            total += cell_total
    print(f"projected CPU seconds (final counts): {total:,.0f}; wall at 10 workers ~ {total / 10 / 60:,.1f} min")


# ---------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="dev", choices=["final", "dev", "test"])
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--cells", nargs="*", default=None)
    ap.add_argument("--sizes", nargs="*", type=int, default=None)
    ap.add_argument("--benchmark", action="store_true", help="timing only; prints no estimator output")
    args = ap.parse_args(argv)

    cfg_bytes = CONFIG_PATH.read_bytes()
    cfg = yaml.safe_load(cfg_bytes)
    meth = methodology_state(cfg)
    if not (meth["freeze_commit_exists"] and meth["document_unchanged_since_freeze"]):
        print(f"refusing to run: methodology not at its frozen state: {meth}", file=sys.stderr)
        return 1
    strengths = cal.solve_strengths(cfg)
    if args.benchmark:
        benchmark(cfg, strengths)
        return 0

    ctx = cal.make_context(cfg, args.profile, strengths)
    out_dir = Path(args.out_dir) if args.out_dir else REPORT_ROOT / (
        "calibration_v1" if args.profile == "final" else f"calibration_v1_{args.profile}")
    partial = args.cells is not None or args.sizes is not None
    if args.profile == "final" and partial:
        print("the final profile always runs every cell and size", file=sys.stderr)
        return 1
    started = dt.datetime.now(dt.timezone.utc)
    impl = implementation_state()
    t_run = time.perf_counter()

    def progress(done, total, cid, n, secs):
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {done}/{total} {cid} n={n} "
              f"task {secs:.0f}s, elapsed {time.perf_counter() - t_run:.0f}s", file=sys.stderr, flush=True)

    with real_data_guard(REPO_ROOT):
        res = cal.run_calibration(ctx, str(REPO_ROOT), workers=args.workers, cell_ids=args.cells,
                                  sizes=args.sizes, progress=progress)
    rows = res["rows"]
    i_ref = cal.reference_mi_bits(cfg)
    decisions = decide(rows, cfg, i_ref) if not partial else {"note": "partial run: no decisions"}

    out_dir.mkdir(parents=True, exist_ok=True)
    tables = split_tables(rows)
    tables["scenario_truth.csv"] = scenario_truth_rows(ctx)
    tables["all_rows.csv"] = rows
    for name, trs in tables.items():
        write_csv(out_dir / name, trs)
    summary = {
        "experiment_id": cfg["experiment_id"], "profile": args.profile, "synthetic_only": True,
        "real_market_mi_computed": False,
        "config_sha256": sha256_bytes(cfg_bytes), "config_canonical_sha256": sha256_bytes(canonical_json(cfg).encode()),
        "methodology": meth, "counts": ctx.counts, "i_ref_bits": i_ref, "strengths": strengths,
        "decisions": decisions,
    }
    (out_dir / "calibration_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {p.name: sha256_bytes(p.read_bytes()) for p in sorted(out_dir.iterdir())
               if p.is_file() and p.name != "run_metadata.json"}
    combined = sha256_bytes(canonical_json(outputs).encode())
    meta = {
        "dataset": "information_decay_calibration_v1", "profile": args.profile, "synthetic_only": True,
        "real_market_mi_computed": False, "real_data_guard": "isolation.real_data_guard (main process and every worker)",
        "generated_at_utc": started.isoformat(timespec="seconds"),
        "config": {"path": str(CONFIG_PATH.relative_to(REPO_ROOT)), "sha256": summary["config_sha256"],
                   "canonical_sha256": summary["config_canonical_sha256"]},
        "methodology": meth, "implementation": impl, "seed_scheme": "sha256('m3_information_decay|v1|calibration|<cell>|<n>|<rep>|<purpose>')[:8] -> PCG64",
        "counts": ctx.counts, "workers": args.workers, "tasks": res["tasks"],
        "wall_seconds": round(res["wall_seconds"], 1),
        "cpu_seconds_by_cell": {k: round(v, 1) for k, v in sorted(res["cpu_seconds_by_cell"].items())},
        "outputs": {"directory": str(out_dir.relative_to(REPO_ROOT)) if out_dir.is_relative_to(REPO_ROOT) else str(out_dir),
                    "files_sha256": outputs, "combined_sha256": combined},
    }
    if args.profile == "final":
        METADATA_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    else:
        (out_dir / "run_metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(outputs)} files to {out_dir} (combined sha256 {combined}); wall {res['wall_seconds']:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
