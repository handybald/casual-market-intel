"""Amendment 02 decision rules (docs/research/information_decay_v1_amendment_02.md).

Pure functions over SYNTHETIC calibration counts and rows; nothing here reads real data.
`decisions.py` keeps the Amendment-01 rules unchanged for audit reproducibility; this module
supersedes them for every decision Amendment 02 revises.

Monte Carlo rule (Amendment 02 §4): one-sided Wilson score tests of each hard cell's panel
type-I rate against its frozen ceiling c, Holm step-down across the still-undecided cells at
alpha_look = family_alpha / max_looks, PASS / FAIL locked once decided, INCONCLUSIVE cells get
fixed batches of extra replicates up to max_reps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"


def ceiling(reference_reps: int, alpha: float = 0.05, mult: float = 2.0) -> float:
    return alpha + mult * math.sqrt(alpha * (1 - alpha) / reference_reps)


def normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def wilson_score_z(k: int, R: int, c: float) -> float:
    """Score statistic of H: p = c (null variance): inverting it gives the Wilson interval."""
    return (k / R - c) / math.sqrt(c * (1 - c) / R)


def holm_reject(pvalues: Dict[Any, float], alpha: float) -> set:
    """Holm step-down: the set of hypotheses rejected at family level alpha."""
    order = sorted(pvalues, key=lambda key: (pvalues[key], str(key)))
    m = len(order)
    rejected = set()
    for i, key in enumerate(order):
        if pvalues[key] <= alpha / (m - i):
            rejected.add(key)
        else:
            break
    return rejected


@dataclass
class HardItem:
    gate: str
    cell: str
    estimator: str
    procedure: str
    n: int
    policy: str
    k: int = 0
    R: int = 0
    status: str = INCONCLUSIVE
    decided_look: Optional[int] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str, str, int]:
        return (self.cell, self.estimator, self.procedure, self.n)

    @property
    def decided(self) -> bool:
        return self.status in (PASS, FAIL)


def classify_look(items: Sequence[HardItem], look: int, c: float, alpha_look: float) -> None:
    """One look of the Holm-Wilson rule over the undecided items (mutates items)."""
    und = [it for it in items if not it.decided]
    if not und:
        return
    stats = {}
    for it in und:
        z = wilson_score_z(it.k, it.R, c)
        stats[it.key] = (z, normal_cdf(z), 1.0 - normal_cdf(z))
    passed = holm_reject({k: v[1] for k, v in stats.items()}, alpha_look)
    failed = holm_reject({k: v[2] for k, v in stats.items() if k not in passed}, alpha_look)
    for it in und:
        z, p_pass, p_fail = stats[it.key]
        if it.key in passed:
            it.status, it.decided_look = PASS, look
        elif it.key in failed:
            it.status, it.decided_look = FAIL, look
        it.history.append({"look": look, "R": it.R, "k": it.k, "rate": it.k / it.R, "ceiling": c,
                           "wilson_score_z": z, "p_pass_one_sided": p_pass, "p_fail_one_sided": p_fail,
                           "family_size_this_look": len(und), "alpha_look": alpha_look,
                           "classification": it.status})


def run_sequential(items: List[HardItem], run_batch: Callable[[List[HardItem], int, int], Dict[tuple, Tuple[int, int]]],
                   c: float, alpha_look: float, batch_reps: int, max_reps: int) -> List[HardItem]:
    """Sequential batches: look l uses replicate indices [batch*(l-1), batch*l) for every item that
    is still undecided. `run_batch(undecided, rep_start, rep_end)` returns {item.key: (k, R)} for
    that batch only. Stops when nothing is undecided or max_reps is reached."""
    looks = max_reps // batch_reps
    for look in range(1, looks + 1):
        und = [it for it in items if not it.decided]
        if not und:
            break
        a, b = batch_reps * (look - 1), batch_reps * look
        res = run_batch(und, a, b)
        for it in und:
            k_add, r_add = res[it.key]
            if r_add != b - a:
                raise RuntimeError(f"batch for {it.key} returned {r_add} replicates, expected {b - a}")
            it.k += k_add
            it.R += r_add
        classify_look(items, look, c, alpha_look)
    return items


def item_row(it: HardItem) -> Dict[str, Any]:
    last = it.history[-1] if it.history else {}
    return {"gate": it.gate, "cell": it.cell, "estimator": it.estimator, "policy": it.policy,
            "procedure": it.procedure, "n": it.n, "reps": it.R, "rejections": it.k,
            "rate": it.k / it.R if it.R else float("nan"), "ceiling": last.get("ceiling"),
            "wilson_score_z": last.get("wilson_score_z"), "p_pass_one_sided": last.get("p_pass_one_sided"),
            "p_fail_one_sided": last.get("p_fail_one_sided"), "alpha_look": last.get("alpha_look"),
            "looks_used": len(it.history), "decided_look": it.decided_look, "classification": it.status}


# ---------------------------------------------------------------------------------------------
# D3 and qualification
# ---------------------------------------------------------------------------------------------
def policy_status(items: Iterable[HardItem], policy: str) -> str:
    st = [it.status for it in items if it.gate == "V2_D3" and it.policy == policy]
    if not st:
        return INCONCLUSIVE
    if any(s == FAIL for s in st):
        return FAIL
    return PASS if all(s == PASS for s in st) else INCONCLUSIVE


def decide_d3(items: Sequence[HardItem]) -> Dict[str, Any]:
    a, b = policy_status(items, "A"), policy_status(items, "B")
    if a == PASS and b == PASS:
        sel, why = "A", "both pass: A preserves the frozen surprise_std with least transformation"
    elif a == PASS:
        sel, why = "A", "only A passes"
    elif b == PASS:
        sel, why = "B", "only B passes"
    else:
        sel, why = None, "neither policy passes (FAIL or final INCONCLUSIVE): STOP"
    return {"policy_A": a, "policy_B": b, "selected": sel, "rationale": why}


def gate_status(items: Iterable[HardItem], gate: str, policy: Optional[str] = None) -> str:
    st = [it.status for it in items if it.gate == gate and (policy is None or it.policy == policy)]
    if not st:
        return INCONCLUSIVE
    if any(s == FAIL for s in st):
        return FAIL
    return PASS if all(s == PASS for s in st) else INCONCLUSIVE


def v3_evaluate(rows: Sequence[Dict[str, Any]], cells: Sequence[str], sizes: Sequence[int],
                rel_tol: float = 0.10, estimator: str = "gcmi") -> List[Dict[str, Any]]:
    """Revised V3 point rule: mean MI_eff <= truth + 2 SE + rel_tol * truth (replicate-clustered SE)."""
    out = []
    for r in rows:
        if r.get("cell") in cells and int(r.get("n")) in sizes and r.get("estimator") == estimator \
                and r.get("procedure") == "strat":
            t = float(r["truth_bits"])
            lim = t + 2 * float(r["se_eff_bits"]) + rel_tol * t
            m = float(r["mean_eff_bits"])
            out.append({"gate": "V3", "cell": r["cell"], "estimator": estimator, "n": int(r["n"]),
                        "reps": int(r["reps"]), "mean_eff_bits": m, "truth_bits": t,
                        "se_eff_bits": float(r["se_eff_bits"]), "limit_bits": lim,
                        "classification": PASS if m <= lim else FAIL})
    return sorted(out, key=lambda x: (x["cell"], x["n"]))


def v4b_report(rows: Sequence[Dict[str, Any]], cells: Sequence[str], contaminated_truth: Callable[[str, int], float]
               ) -> List[Dict[str, Any]]:
    """Scenario-7 sensitivity: errors against the clean and the contaminated-population targets."""
    idx = {(r["cell"], int(r["n"]), r["estimator"], r["procedure"]): r for r in rows}
    clean_cell = {"s7_alt_contaminated": "s2_rho035_modeA", "s7_null_contaminated": "s1_null_modeA"}
    out = []
    for (cell, n, est, proc), r in sorted(idx.items()):
        if cell not in cells or proc not in ("strat", "none") or not est.startswith(("gcmi", "ksg", "ilin")):
            continue
        t_clean = float(r["truth_bits"])
        t_cont = contaminated_truth(cell, n)
        mo = float(r["mean_obs_bits"])
        me = float(r["mean_eff_bits"]) if r.get("mean_eff_bits") not in (None, "") else None
        shift = idx.get((cell, n, est, "outlier_shift"))
        clean_rej = idx.get((clean_cell[cell], n, est, "strat"))
        out.append({"gate": "V4b", "cell": cell, "estimator": est, "n": n, "reps": int(r["reps"]),
                    "truth_clean_bits": t_clean, "truth_contaminated_bits": t_cont,
                    "mean_obs_bits": mo, "mean_eff_bits": me,
                    "error_eff_vs_clean_bits": None if me is None else me - t_clean,
                    "error_eff_vs_contaminated_bits": None if me is None else me - t_cont,
                    "error_obs_vs_contaminated_bits": mo - t_cont,
                    "median_abs_influence_bits": None if shift is None else float(shift["median_abs_shift_bits"]),
                    "rejection_rate_contaminated": None if r.get("adjusted_rate") in (None, "") else float(r["adjusted_rate"]),
                    "rejection_rate_clean_cell": None if (clean_rej is None or clean_rej.get("adjusted_rate") in (None, ""))
                    else float(clean_rej["adjusted_rate"]),
                    "classification": "SENSITIVITY (not a gate)"})
    return out


def d5_evaluate(cov_rows: Sequence[Dict[str, Any]], cell_rho: Dict[str, float], estimator: str = "gcmi",
                min_cov: float = 0.90, nominal: float = 0.95, min_rho: float = 0.25) -> Dict[str, Any]:
    """Frozen D5 suitability rule on replicate-cluster point coverage."""
    by_m: Dict[str, List[Dict[str, Any]]] = {}
    for r in cov_rows:
        if r["estimator"] == estimator and r["procedure"] == "interval" and r["cell"] in cell_rho:
            by_m.setdefault(r["method"], []).append(r)
    table = {}
    for m, rs in sorted(by_m.items()):
        elig = [r["coverage_eff"] for r in rs if cell_rho[r["cell"]] >= min_rho]
        table[m] = {"cells": len(rs), "min_coverage_rho_ge_0_25": min(elig),
                    "max_coverage": max(r["coverage_eff"] for r in rs),
                    "mean_abs_dev_from_nominal": sum(abs(r["coverage_eff"] - nominal) for r in rs) / len(rs),
                    "eligible": min(elig) >= min_cov}
    eligible = [m for m in table if table[m]["eligible"]]
    order = ["boot_pct", "boot_recentred", "subsample"]
    sel = min(eligible, key=lambda m: (table[m]["mean_abs_dev_from_nominal"], order.index(m))) if eligible else None
    return {"selected": sel, "evidence": table}


def qualification(items: Sequence[HardItem], d3: Dict[str, Any], v3_rows: Sequence[Dict[str, Any]],
                  tests_passed: bool) -> Dict[str, Any]:
    sel = d3["selected"]
    v1 = gate_status(items, "V1")
    v2 = INCONCLUSIVE if sel is None else gate_status(items, "V2_D3", sel)
    v4a = gate_status(items, "V4a")
    v3 = PASS if v3_rows and all(r["classification"] == PASS for r in v3_rows) else FAIL
    ok = (v1 == PASS and v2 == PASS and v3 == PASS and v4a == PASS and sel is not None and tests_passed)
    return {"V1": v1, "V2": v2, "V3": v3, "V4a": v4a, "D3_selected": sel, "tests_passed": tests_passed,
            "gcmi_qualifies": ok,
            "verdict": "GCMI QUALIFIES FOR PRIMARY M3 V1" if ok else "GCMI DOES NOT QUALIFY"}
