#!/usr/bin/env python3
"""Amendment 02 TARGETED synthetic revalidation (SYNTHETIC DATA ONLY).

    python scripts/revalidate_information_decay_amendment02.py --workers 8

Implements docs/research/information_decay_v1_amendment_02.md (committed before any result):
  group A+B  hard-gate family H (V1, D3/V2, V4a): Holm-Wilson PASS/INCONCLUSIVE/FAIL with
             alpha split over looks, 2,000-replicate batches up to 10,000
  group C    secondary residual family R: Null B under the D3-selected tie policy (same rule)
  group D    GCMI coverage cells re-simulated with the original seeds, cluster-aware coverage
  stored     revised V3 and V4b from the Amendment-01 rows (hash-checked)

Refuses to run unless Amendment 02 is committed at its recorded commit and unchanged, and the
methodology is unchanged since its freeze. Runs inside isolation.real_data_guard(); reads no real
surprise / response / residual value.

Outputs: data/reports/information_decay/amendment02_revalidation/ and
metadata/research/information_decay_amendment02_revalidation.json.
"""
from __future__ import annotations

import argparse
import copy
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
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from src.research.information_decay import amendment02 as a2  # noqa: E402
from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay.isolation import real_data_guard  # noqa: E402

A02_CONFIG = REPO_ROOT / "config" / "information_decay_v1_amendment02.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "amendment02_revalidation"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_amendment02_revalidation.json"
CELL_RHO = {"s2_rho015": 0.15, "s2_rho025": 0.25, "s2_rho035_modeA": 0.35, "s2_rho050": 0.50,
            "s3a_copula_monotone": 0.35}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=REPO_ROOT, capture_output=True, text=True)


def frozen_state(path: str, commit: str) -> Dict[str, Any]:
    exists = git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0
    in_commit = exists and git("cat-file", "-e", f"{commit}:{path}").returncode == 0
    unchanged = in_commit and git("diff", "--quiet", commit, "--", path).returncode == 0
    return {"path": path, "commit": commit, "commit_exists": exists, "file_in_commit": in_commit,
            "unchanged_since_commit": unchanged,
            "blob_sha1": git("rev-parse", f"{commit}:{path}").stdout.strip() if in_commit else None}


def implementation_state() -> Dict[str, Any]:
    """Pattern-based scientific implementation fingerprint (Amendment 04 §6): every
    src/research/information_decay/**/*.py, scripts/*information_decay*.py and
    config/information_decay*.yaml file, sorted, per-file SHA-256 plus aggregate."""
    from src.research.information_decay.provenance import implementation_fingerprint
    return implementation_fingerprint(REPO_ROOT)

def fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "nan" if math.isnan(v) else format(v, ".10g")
    return str(v)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    cols: List[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([fmt(r.get(c)) for c in cols])
    path.write_text(buf.getvalue(), encoding="utf-8")


def read_stored_rows(path: Path) -> List[Dict[str, Any]]:
    def conv(v):
        if v == "":
            return None
        if v in ("true", "false"):
            return v == "true"
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            return v
    with open(path, newline="") as fh:
        return [{k: conv(v) for k, v in r.items()} for r in csv.DictReader(fh)]


# ---------------------------------------------------------------------------------------------
class Runner:
    def __init__(self, base_cfg, a02, strengths, workers):
        self.base_cfg, self.a02, self.strengths, self.workers = base_cfg, a02, strengths, workers
        self.base_cells = {c["id"]: c for c in base_cfg["cells"]}
        for c in a02["new_cells"]:
            self.base_cells[c["id"]] = c
        self.all_rows: List[Dict[str, Any]] = []

    def ctx(self, cells: List[Dict[str, Any]]) -> cal.Context:
        cfg = copy.deepcopy(self.base_cfg)
        cfg["cells"] = cells
        cfg["profiles"]["a02"] = dict(self.a02["counts"])
        return cal.make_context(cfg, "a02", self.strengths)

    def run(self, cell_specs: Dict[str, Dict[str, Any]], sizes: Dict[str, List[int]], a: int, b: int, label: str):
        cells = []
        for cid, extra in cell_specs.items():
            c = copy.deepcopy(self.base_cells[cid])
            c.update(extra)
            cells.append(c)
        ctx = self.ctx(cells)
        tasks = [(cid, int(n), a, b) for cid in cell_specs for n in sizes[cid]]
        t0 = time.perf_counter()
        res = cal.run_calibration(ctx, str(REPO_ROOT), workers=self.workers, tasks=tasks)
        print(f"[{dt.datetime.now():%H:%M:%S}] {label}: {len(tasks)} tasks, reps [{a},{b}) "
              f"in {time.perf_counter() - t0:.0f}s", file=sys.stderr, flush=True)
        for r in res["rows"]:
            r["batch_label"] = label
        self.all_rows.extend(res["rows"])
        return res["rows"]


def batch_fn(runner: Runner, policy_of_cell, label: str):
    def run_batch(undecided: List[a2.HardItem], a: int, b: int):
        specs, sizes = {}, {}
        for it in undecided:
            specs.setdefault(it.cell, {"tie_policies": policy_of_cell(it.cell)})
            sizes.setdefault(it.cell, [])
            if it.n not in sizes[it.cell]:
                sizes[it.cell].append(it.n)
        rows = runner.run(specs, sizes, a, b, f"{label} reps [{a},{b})")
        idx = {(r["cell"], r["estimator"], r["procedure"], r["n"]): r for r in rows}
        out = {}
        for it in undecided:
            r = idx[it.key]
            out[it.key] = (int(r["adjusted_rejections"]), int(r["reps"]))
        return out
    return run_batch


def reproduction_checks(new_rows, stored_rows, pairs) -> List[Dict[str, Any]]:
    """Compare first-batch (reps 0..1999) recomputations with the stored Amendment-01 rows."""
    sidx = {(r["cell"], r["n"], r["estimator"], r["procedure"], r.get("method") or ""): r for r in stored_rows}
    out = []
    for r in new_rows:
        if r.get("rep_first", 0) not in (0, None) and r["procedure"] != "interval":
            continue
        for (new_est, stored_est) in pairs.get(r["cell"], []):
            if r["estimator"] != new_est:
                continue
            key = (r["cell"], r["n"], stored_est, r["procedure"], r.get("method") or "")
            s = sidx.get(key)
            if s is None:
                continue
            for fld in ("adjusted_rate", "mean_obs_bits", "mean_null_mean_bits", "mean_eff_bits", "coverage_eff"):
                if fld in r and s.get(fld) is not None:
                    d = abs(float(r[fld]) - float(s[fld]))
                    out.append({"cell": r["cell"], "n": r["n"], "estimator_new": new_est, "estimator_stored": stored_est,
                                "procedure": r["procedure"], "method": r.get("method") or "", "field": fld,
                                "new": float(r[fld]), "stored": float(s[fld]), "abs_diff": d,
                                # stored CSVs carry 10 significant digits
                                "reproduced": d <= 5e-9 * max(1.0, abs(float(s[fld])))})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--skip-tests", action="store_true", help="do not run the calibration test file")
    ap.add_argument("--smoke", default=None, metavar="DIR",
                    help="tiny counts written to DIR; never writes the metadata file (pipeline check only)")
    args = ap.parse_args(argv)
    global OUT_DIR, METADATA

    a02_bytes = A02_CONFIG.read_bytes()
    a02 = yaml.safe_load(a02_bytes)
    base_path = REPO_ROOT / a02["base_config"]
    base_bytes = base_path.read_bytes()
    base_cfg = yaml.safe_load(base_bytes)
    amend = frozen_state(a02["amendment"]["path"], a02["amendment"]["commit"])
    meth = frozen_state(base_cfg["methodology"]["path"], base_cfg["methodology"]["freeze_commit"])
    if not (amend["unchanged_since_commit"] and meth["unchanged_since_commit"]):
        print(f"refusing to run: protocol not at its frozen state: {amend} {meth}", file=sys.stderr)
        return 1
    if args.smoke:
        OUT_DIR = Path(args.smoke)
        METADATA = OUT_DIR / "smoke_metadata.json"
        a02["counts"].update(reps_coverage=3, perms_gcmi=19, perms_ksg=9, boot_gcmi=10, subsample_gcmi=10)
        a02["mc_rule"].update(batch_reps=4, max_reps=8)
        for c_ in a02["new_cells"]:
            c_["reps_ksg"] = 2
    stored_dir = REPO_ROOT / a02["amendment_01"]["outputs_dir"]
    stored_meta = json.loads((REPO_ROOT / "metadata/research/information_decay_calibration_v1.json").read_text())
    stored_ok = stored_meta["outputs"]["combined_sha256"] == a02["amendment_01"]["combined_sha256"] and all(
        sha((stored_dir / f).read_bytes()) == h for f, h in stored_meta["outputs"]["files_sha256"].items())
    if not stored_ok:
        print("refusing to run: stored Amendment-01 outputs do not match their recorded hashes", file=sys.stderr)
        return 1

    tests = {"ran": False}
    if not args.skip_tests:
        p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "tests/test_information_decay_calibration.py"], cwd=REPO_ROOT, capture_output=True, text=True)
        tests = {"ran": True, "returncode": p.returncode, "summary": p.stdout.strip().splitlines()[-1] if p.stdout else ""}
    tests_passed = tests.get("returncode") == 0

    started = dt.datetime.now(dt.timezone.utc)
    impl = implementation_state()
    stored_rows = read_stored_rows(stored_dir / "all_rows.csv")
    mc = a02["mc_rule"]
    c = a2.ceiling(mc["ceiling_reference_reps"], mc["nominal_alpha"])
    sizes = list(a02["hard_sizes"])
    strengths = cal.solve_strengths(base_cfg)
    runner = Runner(base_cfg, a02, strengths, args.workers)

    with real_data_guard(REPO_ROOT):
        # ---------------- groups A + B: hard family H ----------------
        items: List[a2.HardItem] = []
        cell_policies: Dict[str, List[str]] = {}
        for spec in a02["hard_family"]:
            cell_policies[spec["cell"]] = list(spec["policies"])
            for pol in spec["policies"]:
                for n in sizes:
                    items.append(a2.HardItem(gate=spec["gate"], cell=spec["cell"], estimator=cal.GCMI_POLICY_KEYS[pol],
                                             procedure="strat", n=n, policy=pol))
        a2.run_sequential(items, batch_fn(runner, lambda cid: cell_policies[cid], "hard family H"),
                          c, mc["alpha_per_look"], mc["batch_reps"], mc["max_reps"])
        d3 = a2.decide_d3(items)
        sel = d3["selected"]
        if args.smoke and sel is None:
            sel = "A"  # smoke run only: exercise the residual path; never used for a decision

        # ---------------- group C: residual family R ----------------
        r_items: List[a2.HardItem] = []
        if sel is not None:
            est = cal.GCMI_POLICY_KEYS[sel]
            for cid in a02["residual_family"]["cells"]:
                for n in sizes:
                    r_items.append(a2.HardItem(gate="R_nullB", cell=cid, estimator=est,
                                               procedure=a02["residual_family"]["procedure"], n=n, policy=sel))
            a2.run_sequential(r_items, batch_fn(runner, lambda cid: [sel], "residual family R"),
                              c, mc["alpha_per_look"], mc["batch_reps"], mc["max_reps"])
        r_status = a2.gate_status(r_items, "R_nullB") if r_items else "NOT RUN (D3 unresolved)"

        # ---------------- group D: coverage ----------------
        cov_specs = {x["cell"]: {"tie_policies": ["A"]} for x in a02["coverage_cells"]}
        cov_sizes = {x["cell"]: list(x["sizes"]) for x in a02["coverage_cells"]}
        cov_rows_all = runner.run(cov_specs, cov_sizes, 0, int(a02["counts"]["reps_coverage"]), "coverage group D")
    cov_rows = [r for r in cov_rows_all if r["procedure"] == "interval"]

    # ---------------- stored-evidence gates ----------------
    v3_rows = a2.v3_evaluate(stored_rows, a02["v3_cells"], a02["v3_sizes"],
                             base_cfg["decision_rules"]["v3_relative_tolerance"])
    cfg_tmp = runner.ctx([runner.base_cells[c2] for c2 in a02["v4b_cells"]])

    def cont_truth(cell_id, n):
        return cal.cell_contaminated_truth_bits(cfg_tmp, runner.base_cells[cell_id], n)
    v4b_rows = a2.v4b_report(stored_rows, a02["v4b_cells"], cont_truth)
    d5 = a2.d5_evaluate(cov_rows, CELL_RHO, "gcmi", base_cfg["decision_rules"]["d5_min_coverage"],
                        base_cfg["decision_rules"]["d5_nominal"], base_cfg["decision_rules"]["d5_min_rho_for_eligibility"])
    qual = a2.qualification(items, d3, v3_rows, tests_passed)

    # ---------------- reproduction of Amendment-01 values ----------------
    first = [r for r in runner.all_rows if r.get("rep_first") == 0 or r["procedure"] == "interval"]
    pairs = {cid: [("gcmi", "gcmi"), ("gcmi_B", "gcmi_B")] for cid in cell_policies}
    for cid in a02["residual_family"]["cells"]:
        pairs[cid] = [("gcmi", "gcmi")] + ([("gcmi_B", "gcmi")] if cid == "s8_null_t3" else [])
    for cid in cov_specs:
        pairs[cid] = [("gcmi", "gcmi")]
    repro = reproduction_checks(first, stored_rows, pairs)

    # ---------------- artifacts ----------------
    hard_rows = [a2.item_row(it) for it in items]
    res_rows = [a2.item_row(it) for it in r_items]
    look_rows = [dict(gate=it.gate, cell=it.cell, estimator=it.estimator, policy=it.policy, procedure=it.procedure,
                      n=it.n, **h) for it in items + r_items for h in it.history]
    diag_rows = [r for r in runner.all_rows if r["cell"] in a02["residual_family"]["cells"]
                 and r["procedure"] in a02["residual_family"]["diagnostic_procedures"]]
    v4a_ksg = [r for r in runner.all_rows if r["cell"] == "s7v4a_null_independent_contamination"
               and r["estimator"].startswith("ksg") and r.get("rep_first") == 0]
    flagged = [r for r in hard_rows + res_rows if r["classification"] != a2.PASS] + \
              [dict(r, classification=r["classification"]) for r in v3_rows if r["classification"] != a2.PASS]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tables = {"hard_gate_cells.csv": hard_rows, "residual_family_cells.csv": res_rows, "look_history.csv": look_rows,
              "residual_diagnostics_nullA_matched_raw.csv": diag_rows, "v3_cells.csv": v3_rows,
              "v4b_sensitivity.csv": v4b_rows, "v4a_ksg_descriptive.csv": v4a_ksg,
              "coverage_cluster.csv": cov_rows, "reproduction_checks.csv": repro,
              "flagged_cells.csv": flagged, "all_rows.csv": runner.all_rows}
    for name, rows in tables.items():
        write_csv(OUT_DIR / name, rows)
    decisions = {
        "experiment_id": base_cfg["experiment_id"], "amendment": "02", "synthetic_only": True,
        "real_market_mi_computed": False,
        "mc_rule": dict(mc, ceiling=c),
        "D3": d3, "qualification": qual,
        "residual_family_R": {"status": r_status, "policy": sel},
        "D5": d5,
        "reproduction": {"checks": len(repro), "all_reproduced": all(r["reproduced"] for r in repro),
                         "not_reproduced": [r for r in repro if not r["reproduced"]]},
        "flagged_items": flagged,
        "counts_by_classification": {g: {s: sum(1 for r in hard_rows if r["gate"] == g and r["classification"] == s)
                                         for s in (a2.PASS, a2.INCONCLUSIVE, a2.FAIL)}
                                     for g in sorted({r["gate"] for r in hard_rows})},
        "tests": tests,
    }
    (OUT_DIR / "decisions.json").write_text(json.dumps(decisions, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {p.name: sha(p.read_bytes()) for p in sorted(OUT_DIR.iterdir()) if p.is_file()}
    meta = {"dataset": "information_decay_amendment02_revalidation", "synthetic_only": True,
            "real_market_mi_computed": False, "generated_at_utc": started.isoformat(timespec="seconds"),
            "amendment_02": amend, "methodology": meth,
            "configs": {"base": {"path": a02["base_config"], "sha256": sha(base_bytes)},
                        "amendment02": {"path": str(A02_CONFIG.relative_to(REPO_ROOT)), "sha256": sha(a02_bytes)}},
            "amendment_01_outputs_verified": stored_ok, "implementation": impl, "workers": args.workers,
            "outputs": {"directory": (str(OUT_DIR.relative_to(REPO_ROOT)) if OUT_DIR.is_relative_to(REPO_ROOT)
                                      else str(OUT_DIR)), "files_sha256": outputs,
                        "combined_sha256": sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"qualification": qual, "D3": d3, "residual": r_status, "D5": d5["selected"],
                      "reproduced": decisions["reproduction"]["all_reproduced"]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
