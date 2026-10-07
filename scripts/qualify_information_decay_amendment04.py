#!/usr/bin/env python3
"""Amendment 04 exact primary-cohort-size qualification (SYNTHETIC DATA ONLY).

    python scripts/qualify_information_decay_amendment04.py --workers 8

Runs ONLY the frozen n = 93 / n = 103 hard FPR cells of
docs/research/information_decay_v1_amendment_04_inventory.json, each with exactly 10,000 synthetic
replicates (indices 0..9,999, frozen seed scheme), one inferential look, and classifies them with
the unchanged Amendment-03 rule (one-sided 95% Wilson upper bound < 0.059746794344808965).

Refuses to run unless the methodology and Amendments 02-04 (+ the OC / inventory files) are at
their committed state, and the programmatically derived inventory equals the committed one. Runs
inside isolation.real_data_guard(); no real surprise / response / residual value is read.
Outputs: data/reports/information_decay/amendment04_exact_size/ and
metadata/research/information_decay_amendment04_exact_size.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from src.research.information_decay import amendment02 as a2  # noqa: E402
from src.research.information_decay import amendment03 as a3  # noqa: E402
from src.research.information_decay import amendment04 as a4  # noqa: E402
from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay.isolation import real_data_guard  # noqa: E402
from src.research.information_decay.provenance import implementation_fingerprint  # noqa: E402

AMENDMENT04 = {"path": "docs/research/information_decay_v1_amendment_04.md",
               "inventory": "docs/research/information_decay_v1_amendment_04_inventory.json",
               "commit": "9f4bef157cd490f848cb3544ab92475def430edc"}
A03_CONFIG = REPO_ROOT / "config" / "information_decay_v1_amendment03.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "amendment04_exact_size"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_amendment04_exact_size.json"

_spec = importlib.util.spec_from_file_location("reval02", REPO_ROOT / "scripts" / "revalidate_information_decay_amendment02.py")
reval02 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reval02)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


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
    fs = reval02.frozen_state
    protocol = {
        "methodology": fs(base_cfg["methodology"]["path"], base_cfg["methodology"]["freeze_commit"]),
        "amendment_02": fs(a02["amendment"]["path"], a02["amendment"]["commit"]),
        "amendment_03": fs(a03["amendment"]["path"], a03["amendment"]["commit"]),
        "amendment_03_oc": fs("docs/research/information_decay_v1_amendment_03_oc.json", a03["amendment"]["commit"]),
        "amendment_04": fs(AMENDMENT04["path"], AMENDMENT04["commit"]),
        "amendment_04_inventory": fs(AMENDMENT04["inventory"], AMENDMENT04["commit"]),
    }
    if not all(s["unchanged_since_commit"] for s in protocol.values()):
        print(f"refusing to run: protocol not at its frozen state: {protocol}", file=sys.stderr)
        return 1
    committed = json.loads((REPO_ROOT / AMENDMENT04["inventory"]).read_text())
    derived = a4.derive_exact_inventory(base_cfg, a02, a03, tuple(int(n) for n in committed["exact_sizes"]))
    if not a4.inventories_equal(derived, committed["cells"]):
        print("refusing to run: derived exact-size inventory differs from the committed inventory", file=sys.stderr)
        return 1
    ceiling = float(committed["rule"]["ceiling"])
    if ceiling != a2.ceiling(a02["mc_rule"]["ceiling_reference_reps"], a02["mc_rule"]["nominal_alpha"]):
        print("refusing to run: committed ceiling differs from the frozen ceiling", file=sys.stderr)
        return 1
    n_final = int(committed["rule"]["n_final"])

    p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_information_decay_calibration.py"], cwd=REPO_ROOT, capture_output=True, text=True)
    tests = {"returncode": p.returncode, "summary": p.stdout.strip().splitlines()[-1] if p.stdout else ""}
    if p.returncode != 0:
        print(f"refusing to run: calibration tests fail: {tests}", file=sys.stderr)
        return 1
    started = dt.datetime.now(dt.timezone.utc)
    impl = implementation_fingerprint(REPO_ROOT)

    runner = reval02.Runner(base_cfg, a02, cal.solve_strengths(base_cfg), args.workers)
    pol_by_cell = {}
    for c in derived:
        pol_by_cell.setdefault(c["cell"], set()).add(c["tie_policy"])
    with real_data_guard(REPO_ROOT):
        cells = []
        for cid, pols in sorted(pol_by_cell.items()):
            c = dict(runner.base_cells[cid])
            c["tie_policies"] = sorted(pols)
            cells.append(c)
        ctx = runner.ctx(cells)
        tasks = [(c["cell"], c["n"], 0, n_final) for c in derived]
        res = cal.run_calibration(ctx, str(REPO_ROOT), workers=args.workers, tasks=tasks)
    idx = {(r["cell"], r["estimator"], r["procedure"], r["n"]): r for r in res["rows"]}

    table = []
    for c in derived:
        r = idx[(c["cell"], c["estimator"], c["procedure"], c["n"])]
        if not (r["reps"] == n_final and r["rep_first"] == 0 and r["rep_last"] == n_final - 1):
            raise RuntimeError(f"cell {c['cell']} n={c['n']} does not have exactly replicates 0..{n_final - 1}")
        d = a3.classify_cell(int(r["adjusted_rejections"]), int(r["reps"]), ceiling, n_final)
        row = {"gate": c["gate"], "cell": c["cell"], "scenario": c["scenario"], "mode": c["mode"], "n": c["n"],
               "estimator": c["estimator"], "tie_policy": c["tie_policy"], "procedure": c["procedure"], **d,
               "seed_identity": c["seed_identity"], "replicate_indices": f"0-{n_final - 1}",
               "required": True}
        row["row_sha256"] = sha(json.dumps(row, sort_keys=True, default=str).encode())
        table.append(row)
    glob = a3.global_qualification(table)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reval02.write_csv(OUT_DIR / "exact_size_table.csv", table)
    reval02.write_csv(OUT_DIR / "rows.csv", res["rows"])
    decisions = {
        "amendment": "04", "synthetic_only": True, "real_market_mi_computed": False,
        "exact_sizes": committed["exact_sizes"], "cells": len(table), "passed": glob["passed"],
        "failed_cells": glob["failed_cells"],
        "counts_by_gate": {g: {str(n): sum(1 for t in table if t["gate"] == g and t["n"] == n) for n in (93, 103)}
                           for g in ("V1", "V2_D3", "V4a")},
        "rule": committed["rule"], "tests": tests,
        "primary_cohort_result": "PRIMARY SPY COHORT SIZES QUALIFY" if glob["qualifies"]
        else "PRIMARY SPY COHORT QUALIFICATION FAILED",
        "gcmi_status": "GCMI QUALIFIES FOR PRIMARY M3 V1 ON THE ACTUAL PRIMARY COHORT SIZES" if glob["qualifies"]
        else "GCMI DOES NOT QUALIFY",
    }
    (OUT_DIR / "decisions.json").write_text(json.dumps(decisions, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {q.name: sha(q.read_bytes()) for q in sorted(OUT_DIR.iterdir()) if q.is_file()}
    meta = {"dataset": "information_decay_amendment04_exact_size", "synthetic_only": True,
            "real_market_mi_computed": False, "generated_at_utc": started.isoformat(timespec="seconds"),
            "protocol": protocol, "inventory_sha256": sha((REPO_ROOT / AMENDMENT04["inventory"]).read_bytes()),
            "implementation_fingerprint": impl,
            "configs": {"amendment03": sha(a03_bytes), "amendment02": sha(a02_bytes), "base": sha(base_bytes)},
            "workers": args.workers, "wall_seconds": round(res["wall_seconds"], 1),
            "outputs": {"directory": str(OUT_DIR.relative_to(REPO_ROOT)), "files_sha256": outputs,
                        "combined_sha256": sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: decisions[k] for k in ("primary_cohort_result", "gcmi_status", "cells", "passed",
                                                "counts_by_gate")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
