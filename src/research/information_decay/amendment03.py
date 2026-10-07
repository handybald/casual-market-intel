"""Amendment 03 fixed-sample Monte Carlo qualification rule
(docs/research/information_decay_v1_amendment_03.md). Supersedes the Amendment-02 sequential
Holm-Wilson qualification mechanism; every other Amendment-02 decision is unchanged.

Rule. Each required hard type-I cell i has exactly N_FINAL = 10,000 synthetic replicates and ONE
inferential look. With k_i false positives, the cell PASSES iff the one-sided 95% Wilson upper
confidence bound for its true FPR p_i is below the frozen ceiling c = 0.0597468; otherwise it
FAILS. There is no INCONCLUSIVE class. GCMI qualifies iff every required cell passes.

Intersection-union test. H0_i: p_i >= c. Qualification rejects H0_global = union_i H0_i. Under any
configuration in H0_global some H0_j is true, so
  P(qualify) = P(reject every H0_i) <= P(reject H0_j) <= alpha,
because each per-cell test is a level-alpha test of H0_j. No cross-cell multiplicity adjustment
is required (Berger 1982; Casella & Berger, Statistical Inference, §8.2.3).

Pure functions over SYNTHETIC counts; nothing here reads real data.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

N_FINAL = 10_000
Z_ONE_SIDED_95 = 1.6448536269514722
PASS, FAIL = "PASS", "FAIL"


def wilson_upper_one_sided(k: int, n: int, z: float = Z_ONE_SIDED_95) -> float:
    """One-sided (1 - alpha) Wilson score upper confidence bound for a binomial proportion."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= k <= n:
        raise ValueError("k must be in [0, n]")
    p = k / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return min(1.0, (centre + half) / (1 + z2 / n))


def classify_cell(k: int, n: int, ceiling: float, n_required: int = N_FINAL) -> Dict[str, Any]:
    """Fixed-sample cell decision. Refuses any replicate count other than the frozen N: there is
    no early (or late) qualification."""
    if n != n_required:
        raise ValueError(f"fixed-sample rule requires exactly {n_required} replicates, got {n}")
    ub = wilson_upper_one_sided(k, n)
    return {"reps": n, "false_positives": k, "p_hat": k / n, "wilson_upper_95_one_sided": ub,
            "ceiling": ceiling, "classification": PASS if ub < ceiling else FAIL}


def max_passing_count(n: int, ceiling: float) -> int:
    """Largest k whose one-sided Wilson upper bound is < ceiling (-1 if none)."""
    k = -1
    while k + 1 <= n and wilson_upper_one_sided(k + 1, n) < ceiling:
        k += 1
    return k


def global_qualification(cells: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Intersection-union decision: qualify iff EVERY required cell passes. No Holm/Bonferroni."""
    req = [c for c in cells if c.get("required", True)]
    if not req:
        return {"qualifies": False, "reason": "no required cells"}
    failed = [c for c in req if c["classification"] != PASS]
    return {"qualifies": not failed, "required_cells": len(req), "passed": len(req) - len(failed),
            "failed_cells": failed}


# ---------------------------------------------------------------------------------------------
# operating characteristics (binomial simulation only -- no estimator is simulated)
# ---------------------------------------------------------------------------------------------
def _binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(k + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def exact_qualification_probability(true_p: Sequence[float], n: int, ceiling: float) -> float:
    kmax = max_passing_count(n, ceiling)
    out = 1.0
    for p in true_p:
        out *= _binom_cdf(kmax, n, p)
    return out


def simulate_new_rule(true_p: Sequence[float], n: int, ceiling: float, sims: int,
                      rng: np.random.Generator, chunk: int = 100_000) -> float:
    """Monte Carlo P(all cells PASS) under the fixed-sample one-sided Wilson rule."""
    kmax = max_passing_count(n, ceiling)
    p = np.asarray(true_p, dtype=float)
    hits = 0
    done = 0
    while done < sims:
        m = min(chunk, sims - done)
        k = rng.binomial(n, p, size=(m, len(p)))
        hits += int(np.all(k <= kmax, axis=1).sum())
        done += m
    return hits / sims


def simulate_amendment02_rule(true_p: Sequence[float], required: Sequence[bool], ceiling: float, sims: int,
                              rng: np.random.Generator, batch: int = 2000, looks: int = 5,
                              alpha_look: float = 0.01) -> float:
    """Monte Carlo P(all REQUIRED cells PASS) under the superseded Amendment-02 sequential
    Holm-Wilson rule (alpha split over looks), using the Amendment-02 implementation itself."""
    from .amendment02 import HardItem, classify_look, PASS as P2
    p = np.asarray(true_p, dtype=float)
    req = np.asarray(required, dtype=bool)
    hits = 0
    for _ in range(sims):
        items = [HardItem("X", f"c{i}", "gcmi", "strat", 0, "A") for i in range(len(p))]
        for look in range(1, looks + 1):
            und = [i for i, it in enumerate(items) if not it.decided]
            if not und:
                break
            draws = rng.binomial(batch, p[und])
            for j, i in enumerate(und):
                items[i].k += int(draws[j])
                items[i].R += batch
            classify_look(items, look, ceiling, alpha_look)
        hits += all(items[i].status == P2 for i in range(len(p)) if req[i])
    return hits / sims


def oc_configurations(n_cells: int = 42, n_required: int = 36) -> List[Dict[str, Any]]:
    """Configurations evaluated before reclassification. Cells 0..n_required-1 are the required
    qualification cells; any perturbed cell is a required cell."""
    def cfg(name, perturb):
        p = [0.05] * n_cells
        for i, v in perturb:
            p[i] = v
        return {"name": name, "true_p": p}
    c = 0.05 + 2 * math.sqrt(0.05 * 0.95 / 2000)
    return [
        cfg("A: all cells p = 0.05", []),
        cfg("A': all cells p = 0.045", [(i, 0.045) for i in range(n_cells)]),
        cfg("A'': all cells p = 0.053", [(i, 0.053) for i in range(n_cells)]),
        cfg("B: one cell p = ceiling (0.0597), others 0.05", [(0, c)]),
        cfg("C1: one cell p = 0.065", [(0, 0.065)]),
        cfg("C2: one cell p = 0.07", [(0, 0.07)]),
        cfg("C3: one cell p = 0.08", [(0, 0.08)]),
        cfg("D1: two cells p = ceiling", [(0, c), (1, c)]),
        cfg("D2: three cells p = ceiling", [(0, c), (1, c), (2, c)]),
        cfg("D3: one ceiling + one 0.065", [(0, c), (1, 0.065)]),
        cfg("D4: all required cells p = ceiling", [(i, c) for i in range(n_required)]),
    ]


def run_operating_characteristics(ceiling: float, sims_new: int = 1_000_000, sims_old: int = 20_000,
                                  seed_label: str = "amendment03_oc", n_cells: int = 42,
                                  n_required: int = 36) -> Dict[str, Any]:
    from .seeds import rng_for
    required = [i < n_required for i in range(n_cells)]
    rows = []
    for cfg in oc_configurations(n_cells, n_required):
        p_req = [p for p, r in zip(cfg["true_p"], required) if r]
        rng_new = rng_for(seed_label, "new_rule", cfg["name"])
        rng_new42 = rng_for(seed_label, "new_rule_all42", cfg["name"])
        rng_old = rng_for(seed_label, "amendment02_rule", cfg["name"])
        rows.append({
            "configuration": cfg["name"],
            "new_rule_P_qualify_required36_sim": simulate_new_rule(p_req, N_FINAL, ceiling, sims_new, rng_new),
            "new_rule_P_qualify_required36_exact": exact_qualification_probability(p_req, N_FINAL, ceiling),
            "new_rule_P_all42_pass_sim": simulate_new_rule(cfg["true_p"], N_FINAL, ceiling, sims_new, rng_new42),
            "amendment02_rule_P_qualify_required36_sim": simulate_amendment02_rule(
                cfg["true_p"], required, ceiling, sims_old, rng_old),
        })
    return {"ceiling": ceiling, "n_final": N_FINAL, "z_one_sided_95": Z_ONE_SIDED_95,
            "max_passing_false_positives": max_passing_count(N_FINAL, ceiling),
            "sims_new_rule": sims_new, "sims_amendment02_rule": sims_old,
            "cells_total": n_cells, "cells_required": n_required, "seed_label": seed_label,
            "seed_scheme": "rng_for(seed_label, rule, configuration) = PCG64(SeedSequence(sha256(...)[:8]))",
            "rows": rows}
