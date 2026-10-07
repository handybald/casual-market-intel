"""Mechanical application of the pre-specified calibration decision rules D1-D8.

Rules: methodology §10 (frozen at the freeze commit) as operationalized in
config/information_decay_v1.yaml `decision_rules`. This module only reads aggregated SYNTHETIC
calibration rows; it never sees real data. Hierarchy: type-I > bias > stability/RMSE >
robustness > power -- the most powerful but miscalibrated option loses.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

TYPE_I_CELLS = ("s1_null_modeA", "s1_null_modeB", "s6_null_t3", "s10_regime_null")
V3_CELLS = ("s2_rho015", "s2_rho025", "s2_rho035_modeA", "s2_rho050", "s3a_copula_monotone",
            "s3b_monotone_tanh", "s6_alt_t3", "s7_alt_contaminated")
OUTLIER_CELLS = ("s7_null_contaminated", "s7_alt_contaminated")
RESIDUAL_NULL_CELLS = ("s8_null_modeA", "s8_null_modeB_cont", "s8_null_modeB_disc", "s8_null_t3")
COVERAGE_CELLS = ("s2_rho015", "s2_rho025", "s2_rho035_modeA", "s2_rho050", "s3a_copula_monotone")
CELL_RHO = {"s2_rho015": 0.15, "s2_rho025": 0.25, "s2_rho035_modeA": 0.35, "s2_rho050": 0.50,
            "s3a_copula_monotone": 0.35}
GCMI_POLICIES = (("A", "gcmi"), ("B", "gcmi_B"), ("C", "gcmi_C"))
KSG_POLICIES = (("K-A", ""), ("K-B", "_KB"))


class Rows:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.idx: Dict[tuple, Dict[str, Any]] = {}
        for r in rows:
            self.idx[(r["cell"], r["n"], r["estimator"], r["procedure"], r.get("method", ""))] = r

    def get(self, cell, n, est, proc, method="") -> Optional[Dict[str, Any]]:
        return self.idx.get((cell, n, est, proc, method))


def _ok_rate(r: Optional[Dict[str, Any]]) -> Optional[bool]:
    if r is None or "adjusted_rate" not in r:
        return None
    return r["adjusted_rate"] <= r["acceptance_max"] + 1e-12


def _all_true(vals) -> bool:
    vals = list(vals)
    return bool(vals) and all(v is True for v in vals)


def decide(rows: List[Dict[str, Any]], config: Dict[str, Any], i_ref: float) -> Dict[str, Any]:
    R = Rows(rows)
    dr = config["decision_rules"]
    nmin_valid = int(dr["min_n_for_validity"])
    grid = [n for n in config["sample_sizes"]["grid"]]
    gridv = [n for n in grid if n >= nmin_valid]
    exact = list(config["sample_sizes"]["exact_cohort_sizes"])
    ks = list(config["estimators"]["ksg_k"])
    out: Dict[str, Any] = {"i_ref_bits": i_ref, "validity_sizes": gridv}

    # ---------------- D3: tie policy (scenario 9 null) ----------------
    def policy_pass(est: str) -> Dict[int, Optional[bool]]:
        return {n: _ok_rate(R.get("s9_null_discrete", n, est, "strat")) for n in gridv}

    def null_stats(est: str, n: int):
        r = R.get("s9_null_discrete", n, est, "strat")
        return (r["mean_obs_bits"], r["sd_obs_bits"], r["sd_obs_bits"] / math.sqrt(r["reps"])) if r else None

    def choose_policy(candidates: Sequence[tuple]) -> Dict[str, Any]:
        table = {name: {"estimators": ests, "type_i_pass_by_n": {n: [policy_pass(e)[n] for e in ests] for n in gridv}}
                 for name, ests in candidates}
        passing = [name for name, ests in candidates
                   if all(_all_true(table[name]["type_i_pass_by_n"][n]) for n in gridv)]
        chosen = passing[0] if passing else None
        if chosen is not None:
            for later in passing[passing.index(chosen) + 1:]:
                better_everywhere = True
                for n in gridv:
                    for e0, e1 in zip(dict(candidates)[chosen], dict(candidates)[later]):
                        a, b = null_stats(e0, n), null_stats(e1, n)
                        if not (a and b and (a[0] - b[0]) > 2 * math.hypot(a[2], b[2]) and b[1] <= a[1]):
                            better_everywhere = False
                if better_everywhere:
                    chosen = later
                    break
        return {"selected": chosen, "passing": passing, "evidence": table}

    d3_g = choose_policy([(name, [est]) for name, est in GCMI_POLICIES])
    d3_k = choose_policy([(name, [f"ksg{k}{suf}" for k in ks]) for name, suf in KSG_POLICIES])
    gcmi_key = dict(GCMI_POLICIES).get(d3_g["selected"]) if d3_g["selected"] else None
    ksg_suffix = dict(KSG_POLICIES).get(d3_k["selected"]) if d3_k["selected"] else None
    out["D3"] = {"gcmi_policy": d3_g["selected"], "ksg_policy": d3_k["selected"], "gcmi": d3_g, "ksg": d3_k}

    # ---------------- validity criteria V1-V4 ----------------
    def v1(est: str, n: int) -> bool:
        return _all_true(_ok_rate(R.get(c, n, est, "strat")) for c in TYPE_I_CELLS)

    def v2(est: str, policy_est: Optional[str], n: int) -> bool:
        return policy_est is not None and _ok_rate(R.get("s9_null_discrete", n, policy_est, "strat")) is True

    def v3(est: str, n: int) -> Dict[str, Any]:
        detail = {}
        for c in V3_CELLS:
            r = R.get(c, n, est, "strat")
            if r is None or r.get("truth_bits") is None:
                continue
            t = r["truth_bits"]
            lim = t + 2 * r["se_eff_bits"] + float(dr["v3_relative_tolerance"]) * t
            detail[c] = {"mean_eff_bits": r["mean_eff_bits"], "truth_bits": t, "limit_bits": lim,
                         "pass": r["mean_eff_bits"] <= lim}
        return {"pass": bool(detail) and all(d["pass"] for d in detail.values()), "cells": detail}

    def v4(est: str, n: int) -> Dict[str, Any]:
        detail = {}
        for c in OUTLIER_CELLS:
            a = R.get(c, n, est, "outlier_shift")
            b = R.get(c, n, "ksg5", "outlier_shift")
            if a and b:
                detail[c] = {"median_abs_shift_bits": a["median_abs_shift_bits"],
                             "ksg5_median_abs_shift_bits": b["median_abs_shift_bits"],
                             "pass": a["median_abs_shift_bits"] <= b["median_abs_shift_bits"] + 1e-15}
        return {"pass": bool(detail) and all(d["pass"] for d in detail.values()), "cells": detail}

    def validity(est: str, policy_est: Optional[str]) -> Dict[str, Any]:
        res = {}
        for n in gridv:
            a, b, c, d = v1(est, n), v2(est, policy_est, n), v3(est, n), v4(est, n)
            res[n] = {"V1": a, "V2": b, "V3": c["pass"], "V4": d["pass"], "V3_detail": c["cells"], "V4_detail": d["cells"]}
        return {"by_n": res, "pass": all(v["V1"] and v["V2"] and v["V3"] and v["V4"] for v in res.values())}

    # ---------------- D1: primary estimator ----------------
    cand = [("gcmi", gcmi_key)] + [(f"ksg{k}", (f"ksg{k}{ksg_suffix}" if ksg_suffix is not None else None))
                                     for k in sorted(ks, reverse=True)]
    d1_evidence = {est: validity(est, pol) for est, pol in cand}
    primary = None
    if d1_evidence["gcmi"]["pass"]:
        primary = "gcmi"
    else:
        for k in sorted(ks, reverse=True):
            if d1_evidence[f"ksg{k}"]["pass"]:
                primary = f"ksg{k}"
                break
    out["D1"] = {"primary_estimator": primary, "robustness_family": [f"ksg{k}" for k in ks],
                 "evidence": d1_evidence,
                 "stop_real_data": primary is None}
    if primary is None:
        return out
    primary_policy_est = gcmi_key if primary == "gcmi" else f"{primary}{ksg_suffix}"

    # ---------------- D4: residual null ----------------
    d4 = {"by_cell": {}}
    for c in RESIDUAL_NULL_CELLS:
        d4["by_cell"][c] = {n: {proc: (R.get(c, n, primary, proc) or {}).get("adjusted_rate")
                                for proc in ("nullA", "nullB", "matched_raw")}
                            | {"acceptance_max": (R.get(c, n, primary, "nullA") or {}).get("acceptance_max")}
                            for n in gridv}
    a_ok = all(_ok_rate(R.get(c, n, primary, "nullA")) is True for c in RESIDUAL_NULL_CELLS for n in gridv)
    b_ok = all(_ok_rate(R.get(c, n, primary, "nullB")) is True for c in RESIDUAL_NULL_CELLS for n in gridv)
    if a_ok:
        d4.update(residual_primary=True, residual_null="nullA_fixed_residual",
                  rationale="Null A calibrated in every scenario-8 null variant; simpler procedure preferred"
                            + ("" if b_ok else " (Null B not calibrated)"))
    elif b_ok:
        d4.update(residual_primary=True, residual_null="nullB_pipeline_aware",
                  rationale="Null A inflates type-I in at least one scenario-8 null variant; Null B calibrated")
    else:
        d4.update(residual_primary=False, residual_null=None,
                  rationale="neither residual null calibrated: raw MI becomes primary (methodology §10.3 D4)")
    d4.update(nullA_calibrated=a_ok, nullB_calibrated=b_ok)
    d4["residual_pipeline_power_info"] = {
        n: {proc: (R.get("s8_alt_modeA", n, primary, proc) or {}).get("adjusted_rate")
            for proc in ("nullA", "nullB", "matched_raw")} for n in grid}
    out["D4"] = d4

    # ---------------- D6: standardization mode ----------------
    d6 = {"by_n": {}}
    material = False
    for n in grid:
        a1, b1 = R.get("s1_null_modeA", n, primary, "strat"), R.get("s1_null_modeB", n, primary, "strat")
        a2, b2 = R.get("s2_rho035_modeA", n, primary, "strat"), R.get("s2_rho035_modeB", n, primary, "strat")
        if not (a1 and b1 and a2 and b2):
            continue
        t_diff = _ok_rate(a1) != _ok_rate(b1)
        p_diff = abs(a2["adjusted_rate"] - b2["adjusted_rate"])
        e_diff = abs(a2["mean_eff_bits"] - b2["mean_eff_bits"])
        e_tol = 2 * math.hypot(a2["se_eff_bits"], b2["se_eff_bits"])
        m = t_diff or p_diff > float(dr["d6_power_difference"]) or e_diff > e_tol
        material = material or m
        d6["by_n"][n] = {"type_i_A": a1["adjusted_rate"], "type_i_B": b1["adjusted_rate"],
                         "power_A": a2["adjusted_rate"], "power_B": b2["adjusted_rate"],
                         "mean_eff_A": a2["mean_eff_bits"], "mean_eff_B": b2["mean_eff_bits"],
                         "eff_diff_tolerance": e_tol, "material": m}
    d6["canonical_mode"] = "B_pipeline_faithful" if material else "A_direct"
    d6["material_difference"] = material
    out["D6"] = d6

    # ---------------- D2: minimum n ----------------
    def d2_crit(n: int) -> Dict[str, Any]:
        v_1 = v1(primary, n)
        v_2 = v2(primary, primary_policy_est, n)
        pw = {m: (R.get(f"s2_rho035_{m}", n, primary, "strat") or {}).get("adjusted_rate") for m in ("modeA", "modeB")}
        bias = {m: (R.get(f"s2_rho035_{m}", n, primary, "strat") or {}).get("mean_eff_bits") for m in ("modeA", "modeB")}
        sd = {m: (R.get(f"s2_rho035_{m}", n, primary, "strat") or {}).get("sd_eff_bits") for m in ("modeA", "modeB")}
        p_ok = all(v is not None and v >= float(dr["d2_power"]) for v in pw.values())
        b_ok = all(v is not None and abs(v - i_ref) <= float(dr["d2_relative_bias"]) * i_ref for v in bias.values())
        return {"V1": v_1, "V2": v_2, "panel_power": pw, "power_ok": p_ok, "mean_eff_bits": bias,
                "bias_ok": b_ok, "sd_eff_bits": sd, "pointwise_power": {
                    m: (R.get(f"s2_rho035_{m}", n, primary, "strat") or {}).get("pointwise_rate") for m in ("modeA", "modeB")},
                "pass": bool(v_1 and v_2 and p_ok and b_ok)}

    d2_grid = {n: d2_crit(n) for n in grid}
    n_min = next((n for n in grid if d2_grid[n]["pass"]), None)
    d2_exact = {n: d2_crit(n) for n in exact}
    panels = {"SPY CPI_MOM (n=77)": 77, "SPY CORE_CPI_MOM (n=77)": 77, "SPY NFP (n=97)": 97,
              "QQQ CPI_MOM (n=56)": 56, "QQQ CORE_CPI_MOM (n=56)": 56, "QQQ NFP (n=74)": 74}
    out["D2"] = {"n_min": n_min, "grid": d2_grid, "exact": d2_exact,
                 "panel_eligibility": {p: d2_exact[n]["pass"] for p, n in panels.items()}}

    # ---------------- D5: interval method (primary estimator) ----------------
    def interval_choice(est: str, methods: Sequence[str]) -> Dict[str, Any]:
        table = {}
        for m in methods:
            cells = {}
            for c in COVERAGE_CELLS:
                for n in gridv:
                    r = R.get(c, n, est, "interval", m)
                    if r:
                        cells[f"{c}|n={n}"] = {"rho": CELL_RHO[c], "coverage": r["coverage_eff"],
                                               "width": r["mean_width_bits"], "reps": r["reps"]}
            if not cells:
                continue
            elig_cov = [v["coverage"] for v in cells.values() if v["rho"] >= float(dr["d5_min_rho_for_eligibility"])]
            table[m] = {"cells": cells, "min_coverage_rho_ge_0_25": min(elig_cov),
                        "mean_abs_dev_from_nominal": sum(abs(v["coverage"] - float(dr["d5_nominal"])) for v in cells.values()) / len(cells),
                        "eligible": min(elig_cov) >= float(dr["d5_min_coverage"])}
        eligible = [m for m in table if table[m]["eligible"]]
        if eligible:
            chosen = min(eligible, key=lambda m: (table[m]["mean_abs_dev_from_nominal"], methods.index(m)))
            label = "calibrated"
        elif table:
            chosen = max(table, key=lambda m: table[m]["min_coverage_rho_ge_0_25"])
            label = "approximate (under-covering in calibration)"
        else:
            chosen, label = None, "not evaluated"
        return {"selected": chosen, "label": label, "evidence": table}

    out["D5"] = interval_choice(primary, ["boot_pct", "boot_recentred", "subsample"])

    # ---------------- D7: KSG uncertainty ----------------
    d7_methods = {}
    for m in ("boot_pct", "subsample"):
        per_k = {f"ksg{k}": interval_choice(f"ksg{k}", [m])["evidence"].get(m) for k in ks}
        d7_methods[m] = {"per_k": per_k, "eligible_all_k": all(v is not None and v["eligible"] for v in per_k.values())}
    elig = [m for m in d7_methods if d7_methods[m]["eligible_all_k"]]
    if elig:
        chosen = min(elig, key=lambda m: sum(v["mean_abs_dev_from_nominal"] for v in d7_methods[m]["per_k"].values()))
    else:
        chosen = None
    out["D7"] = {"ksg_interval": chosen or "no_formal_ci",
                 "reporting": ("interval: " + chosen) if chosen else
                 "permutation null SD, subsampling spread and k-spread as descriptive sensitivity only",
                 "evidence": d7_methods}

    # ---------------- D8: KSG max-statistic studentization ----------------
    H = config["horizons_min"]
    d8 = {}
    stud = False
    for k in ks:
        for n in gridv:
            r = R.get("s1h_null_hetero", n, f"ksg{k}", "strat")
            if not r:
                continue
            means = [r[f"null_mean_h{h}"] for h in H]
            sds = [r[f"null_sd_h{h}"] for h in H]
            spread = max(means) - min(means)
            sd_mean = sum(sds) / len(sds)
            sd_ratio = max(sds) / min(sds)
            flag = spread > float(dr["d8_null_imbalance"]) * sd_mean or sd_ratio > 1.0 + float(dr["d8_null_imbalance"])
            stud = stud or flag
            d8[f"ksg{k}|n={n}"] = {"null_means": means, "null_sds": sds, "mean_spread_over_sd": spread / sd_mean,
                                   "sd_ratio": sd_ratio, "imbalanced": flag,
                                   "adjusted_rate_raw": r["adjusted_rate"],
                                   "adjusted_rate_studentized": r["adjusted_rate_studentized"]}
    out["D8"] = {"ksg_max_statistic": "studentized" if stud else "raw_mi", "evidence": d8}
    return out
