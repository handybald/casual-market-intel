#!/usr/bin/env python3
"""Amendment 03 fixed-sample qualification (SYNTHETIC DATA ONLY).

    python scripts/qualify_information_decay_amendment03.py --workers 8

1. Verifies Amendment 03 (and Amendment 02, the methodology) are at their committed state and
   that the Amendment-02 outputs match their recorded hashes.
2. Tops every hard cell of the Amendment-02 family up to exactly N_final = 10,000 replicates:
   replicate indices [R_stored, 10,000) only, frozen seed scheme, unchanged simulation code.
3. Classifies each cell ONCE with the one-sided 95% Wilson upper bound (PASS iff U < ceiling),
   then applies the intersection-union global rule over the 36 required cells.

Runs inside isolation.real_data_guard(); reads no real surprise / response / residual value.
Outputs: data/reports/information_decay/amendment03_qualification/ and
metadata/research/information_decay_amendment03_qualification.json.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from src.research.information_decay import amendment02 as a2  # noqa: E402
from src.research.information_decay import amendment03 as a3  # noqa: E402
from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay.isolation import real_data_guard  # noqa: E402

A03_CONFIG = REPO_ROOT / "config" / "information_decay_v1_amendment03.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "amendment03_qualification"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_amendment03_qualification.json"

_spec = importlib.util.spec_from_file_location("reval02", REPO_ROOT / "scripts" / "revalidate_information_decay_amendment02.py")
reval02 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reval02)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_stored_cells(path: Path) -> List[Dict[str, Any]]:
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["n"], r["reps"], r["rejections"] = int(r["n"]), int(r["reps"]), int(r["rejections"])
    return rows


def topup_plan(stored: List[Dict[str, Any]], n_final: int) -> List[Dict[str, Any]]:
    """Replicate ranges still missing: one task per distinct (cell, n, stored R < N_final). Items
    of the same cell stopped at different counts (e.g. two tie policies) get separate ranges, so
    no replicate of any item is counted twice."""
    seen = set()
    out = []
    for r in stored:
        if r["reps"] > n_final:
            raise RuntimeError(f"stored cell exceeds N_final: {r}")
        if r["reps"] < n_final and (r["cell"], r["n"], r["reps"]) not in seen:
            seen.add((r["cell"], r["n"], r["reps"]))
            out.append({"cell": r["cell"], "n": r["n"], "start": r["reps"], "end": n_final})
    return sorted(out, key=lambda e: (e["cell"], e["n"], e["start"]))


def merge_counts(stored: List[Dict[str, Any]], topup_rows: List[Dict[str, Any]], n_final: int) -> List[Dict[str, Any]]:
    """Stored [0, R) counts + the top-up row covering exactly [R, N_final) for that item. Refuses
    anything that is not exactly N_final replicates, or an ambiguous / missing top-up."""
    out = []
    for r in stored:
        k, R = r["rejections"], r["reps"]
        if R < n_final:
            match = [t for t in topup_rows if (t["cell"], t["estimator"], t["procedure"], t["n"]) ==
                     (r["cell"], r["estimator"], r["procedure"], r["n"]) and t["rep_first"] == R]
            if len(match) != 1 or match[0]["rep_last"] != n_final - 1:
                raise RuntimeError(f"no unique top-up [{R},{n_final}) for {r['cell']} n={r['n']} {r['estimator']}")
            k += int(match[0]["adjusted_rejections"])
            R += int(match[0]["reps"])
        if R != n_final:
            raise RuntimeError(f"{r['cell']} n={r['n']} {r['estimator']}: {R} replicates, need exactly {n_final}")
        out.append(dict(r, rejections=k, reps=R, stored_reps=r["reps"], stored_rejections=r["rejections"]))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    a03_bytes = A03_CONFIG.read_bytes()
    a03 = yaml.safe_load(a03_bytes)
    a02_bytes = (REPO_ROOT / a03["amendment02_config"]).read_bytes()
    a02 = yaml.safe_load(a02_bytes)
    base_bytes = (REPO_ROOT / a02["base_config"]).read_bytes()
    base_cfg = yaml.safe_load(base_bytes)
    states = {"amendment_03": reval02.frozen_state(a03["amendment"]["path"], a03["amendment"]["commit"]),
              "amendment_03_oc": reval02.frozen_state("docs/research/information_decay_v1_amendment_03_oc.json",
                                                      a03["amendment"]["commit"]),
              "amendment_02": reval02.frozen_state(a02["amendment"]["path"], a02["amendment"]["commit"]),
              "methodology": reval02.frozen_state(base_cfg["methodology"]["path"], base_cfg["methodology"]["freeze_commit"])}
    if not all(s["unchanged_since_commit"] for s in states.values()):
        print(f"refusing to run: protocol not at its frozen state: {states}", file=sys.stderr)
        return 1
    a02_dir = REPO_ROOT / a03["amendment02_outputs"]["directory"]
    a02_meta = json.loads((REPO_ROOT / a03["amendment02_outputs"]["metadata"]).read_text())
    ok = a02_meta["outputs"]["combined_sha256"] == a03["amendment02_outputs"]["combined_sha256"] and all(
        sha((a02_dir / f).read_bytes()) == h for f, h in a02_meta["outputs"]["files_sha256"].items())
    if not ok:
        print("refusing to run: Amendment-02 outputs do not match their recorded hashes", file=sys.stderr)
        return 1

    p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_information_decay_calibration.py"], cwd=REPO_ROOT, capture_output=True, text=True)
    tests = {"returncode": p.returncode, "summary": p.stdout.strip().splitlines()[-1] if p.stdout else ""}
    started = dt.datetime.now(dt.timezone.utc)
    impl = reval02.implementation_state()
    n_final = int(a03["n_final"])
    ceiling = a2.ceiling(a02["mc_rule"]["ceiling_reference_reps"], a02["mc_rule"]["nominal_alpha"])
    stored = load_stored_cells(a02_dir / "hard_gate_cells.csv")
    plan = topup_plan(stored, n_final)
    policies = {s["cell"]: list(s["policies"]) for s in a02["hard_family"]}

    runner = reval02.Runner(base_cfg, a02, cal.solve_strengths(base_cfg), args.workers)
    with real_data_guard(REPO_ROOT):
        cells = []
        for cid in sorted({e["cell"] for e in plan}):
            c = dict(runner.base_cells[cid])
            c["tie_policies"] = policies[cid]
            cells.append(c)
        ctx = runner.ctx(cells)
        tasks = [(e["cell"], e["n"], e["start"], e["end"]) for e in plan]
        res = cal.run_calibration(ctx, str(REPO_ROOT), workers=args.workers, tasks=tasks)
    topup_rows = [r for r in res["rows"] if r["procedure"] == "strat" and r["estimator"].startswith("gcmi")]
    merged = merge_counts(stored, topup_rows, n_final)

    req = {(x["gate"], x["policy"]) for x in a03["required"]}
    table = []
    for r in merged:
        d = a3.classify_cell(r["rejections"], r["reps"], ceiling, n_final)
        table.append({"gate": r["gate"], "cell": r["cell"], "scenario": runner.base_cells[r["cell"]]["scenario"],
                      "estimator": r["estimator"], "policy": r["policy"], "n": r["n"],
                      "required": (r["gate"], r["policy"]) in req, **d,
                      "stored_reps_amendment02": r["stored_reps"], "topup_reps": n_final - r["stored_reps"]})
    table.sort(key=lambda x: (not x["required"], x["gate"], x["cell"], x["policy"], x["n"]))
    glob = a3.global_qualification(table)
    v3_rows = list(csv.DictReader(open(a02_dir / "v3_cells.csv")))
    v3_pass = bool(v3_rows) and all(r["classification"] == "PASS" for r in v3_rows)
    qualifies = glob["qualifies"] and v3_pass and tests["returncode"] == 0
    gate_summary = {}
    for g in ("V1", "V2_D3", "V4a"):
        rows = [t for t in table if t["gate"] == g and t["required"]]
        gate_summary[g] = {"cells": len(rows), "pass": sum(t["classification"] == "PASS" for t in rows)}
    report_only = [t for t in table if not t["required"]]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reval02.write_csv(OUT_DIR / "qualification_table.csv", table)
    reval02.write_csv(OUT_DIR / "topup_rows.csv", res["rows"])
    decisions = {
        "amendment": "03", "synthetic_only": True, "real_market_mi_computed": False,
        "rule": {"n_final": n_final, "ceiling": ceiling, "wilson": "one-sided 95% upper", "z": a3.Z_ONE_SIDED_95,
                 "max_passing_false_positives": a3.max_passing_count(n_final, ceiling),
                 "global": "intersection-union: qualify iff every required cell passes; no cross-cell adjustment"},
        "gate_summary_required": gate_summary,
        "V3": {"cells": len(v3_rows), "all_pass": v3_pass, "source": "amendment02_revalidation/v3_cells.csv"},
        "required_cells": glob["required_cells"], "required_passed": glob["passed"],
        "failed_required_cells": glob["failed_cells"],
        "report_only_policy_A": {"cells": len(report_only), "pass": sum(t["classification"] == "PASS" for t in report_only),
                                 "note": "D3 not reopened (Amendment 03 §3.1)"},
        "selected_policy": a03["selected_policy"], "tests": tests,
        "verdict": "GCMI QUALIFIES FOR PRIMARY M3 V1" if qualifies else "GCMI DOES NOT QUALIFY",
    }
    (OUT_DIR / "decisions.json").write_text(json.dumps(decisions, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {q.name: sha(q.read_bytes()) for q in sorted(OUT_DIR.iterdir()) if q.is_file()}
    meta = {"dataset": "information_decay_amendment03_qualification", "synthetic_only": True,
            "real_market_mi_computed": False, "generated_at_utc": started.isoformat(timespec="seconds"),
            "protocol": states, "amendment02_outputs_verified": ok, "implementation": impl,
            "configs": {"amendment03": sha(a03_bytes), "amendment02": sha(a02_bytes), "base": sha(base_bytes)},
            "topup_tasks": len(tasks), "topup_replicates": sum(e["end"] - e["start"] for e in plan),
            "workers": args.workers, "wall_seconds": round(res["wall_seconds"], 1),
            "outputs": {"directory": str(OUT_DIR.relative_to(REPO_ROOT)), "files_sha256": outputs,
                        "combined_sha256": sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: decisions[k] for k in ("verdict", "gate_summary_required", "required_passed",
                                                "required_cells", "V3", "report_only_policy_A")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
