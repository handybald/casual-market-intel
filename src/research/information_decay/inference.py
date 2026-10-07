"""Release-level resampling and permutation inference (methodology §11-§13).

All index arrays refer to RELEASES (the statistical unit). A permutation of releases moves a
release's whole response vector (all horizons) together; `release_row_permutations` maps a
release-level permutation onto rows when one release owns several rows (e.g. simultaneous
components or two symbols), so linked rows always move together.

  strata_by_position         chronological strata from fixed proportions (calibration)
  merge_small_strata         methodology §11.2: a stratum with < min_size releases merges into the
                             chronologically next stratum; the last stratum merges backward
  stratified_permutations    (B, n) permutation indices, exchanging only within strata
  unrestricted_permutations  (B, n) unrestricted permutation indices
  permutation_pvalues        p = (1 + #{null >= obs}) / (B + 1), unadjusted per horizon and
                             single-step max-T horizon-adjusted (Westfall-Young)
  stratified_bootstrap       (B, n) with-replacement indices within strata
  stratified_subsample       (B, m) without-replacement indices, m_s = floor(frac * n_s) per stratum
"""
from __future__ import annotations

from typing import Dict, Sequence, Tuple

import numpy as np


def strata_by_position(n: int, proportions: Sequence[float]) -> np.ndarray:
    """Stratum label 0..S-1 for releases 0..n-1 in time order; sizes floor(p*n), remainder last."""
    sizes = [int(np.floor(p * n)) for p in proportions[:-1]]
    sizes.append(n - sum(sizes))
    return np.repeat(np.arange(len(sizes)), sizes)


def merge_small_strata(labels: np.ndarray, min_size: int = 8) -> np.ndarray:
    """Relabel so every stratum has >= min_size members (if the total allows). Labels must be
    chronological (0 = earliest). Returns consecutive labels 0..S'-1."""
    labels = np.asarray(labels)
    groups = [list(np.flatnonzero(labels == s)) for s in np.unique(labels)]
    changed = True
    while changed and len(groups) > 1:
        changed = False
        for i, g in enumerate(groups):
            if len(g) < min_size:
                if i < len(groups) - 1:
                    groups[i + 1] = sorted(g + groups[i + 1])
                else:
                    groups[i - 1] = sorted(groups[i - 1] + g)
                del groups[i]
                changed = True
                break
    out = np.empty(len(labels), dtype=int)
    for s, g in enumerate(groups):
        out[g] = s
    return out


def stratified_permutations(labels: np.ndarray, B: int, rng: np.random.Generator) -> np.ndarray:
    labels = np.asarray(labels)
    n = len(labels)
    out = np.empty((B, n), dtype=np.int64)
    for s in np.unique(labels):
        idx = np.flatnonzero(labels == s)
        out[:, idx] = rng.permuted(np.broadcast_to(idx, (B, len(idx))), axis=1)
    return out


def unrestricted_permutations(n: int, B: int, rng: np.random.Generator) -> np.ndarray:
    return rng.permuted(np.broadcast_to(np.arange(n), (B, n)), axis=1)


def release_row_permutations(row_release: np.ndarray, release_perm: np.ndarray) -> np.ndarray:
    """Map release-level permutations onto rows.

    row_release: (n_rows,) release index 0..R-1 of each row; release_perm: (B, R) giving, for
    target release r, the source release whose label it receives. Rows of the same release share
    one source, and linked rows (same release, same within-release position) move together.
    Returns (B, n_rows) source-row indices; requires each release to have the same row layout
    (e.g. one row per symbol), identified by position within the release."""
    row_release = np.asarray(row_release)
    R = release_perm.shape[1]
    pos = np.zeros(len(row_release), dtype=int)
    lookup: Dict[Tuple[int, int], int] = {}
    seen: Dict[int, int] = {}
    for i, r in enumerate(row_release):
        pos[i] = seen.get(int(r), 0)
        seen[int(r)] = pos[i] + 1
        lookup[(int(r), int(pos[i]))] = i
    if len(set(seen.values())) != 1 or len(seen) != R:
        raise ValueError("every release must own the same number of rows")
    table = np.empty((R, max(seen.values())), dtype=np.int64)
    for (r, p), i in lookup.items():
        table[r, p] = i
    return table[release_perm[:, row_release], pos[None, :]]


def permutation_pvalues(obs: np.ndarray, null: np.ndarray) -> Dict[str, np.ndarray]:
    """obs: (H,), null: (B, H). Larger statistic = stronger dependence."""
    obs = np.asarray(obs, dtype=float)
    null = np.asarray(null, dtype=float)
    B = null.shape[0]
    p_unadj = (1 + (null >= obs[None, :]).sum(axis=0)) / (B + 1)
    mx = null.max(axis=1)
    p_adj = (1 + (mx[:, None] >= obs[None, :]).sum(axis=0)) / (B + 1)
    return {"p_unadjusted": p_unadj, "p_horizon_adjusted": p_adj, "p_panel": float(p_adj.min()),
            "null_mean": null.mean(axis=0), "null_sd": null.std(axis=0, ddof=1)}


def studentized_pvalues(obs: np.ndarray, null: np.ndarray) -> Dict[str, np.ndarray]:
    """Max-T on (MI - null mean) / null sd (methodology D8 alternative)."""
    mu = null.mean(axis=0)
    sd = null.std(axis=0, ddof=1)
    sd = np.where(sd > 0, sd, 1.0)
    return permutation_pvalues((obs - mu) / sd, (null - mu) / sd)


def stratified_bootstrap(labels: np.ndarray, B: int, rng: np.random.Generator) -> np.ndarray:
    labels = np.asarray(labels)
    out = np.empty((B, len(labels)), dtype=np.int64)
    for s in np.unique(labels):
        idx = np.flatnonzero(labels == s)
        out[:, idx] = idx[rng.integers(0, len(idx), size=(B, len(idx)))]
    return out


def stratified_subsample(labels: np.ndarray, B: int, frac: float, rng: np.random.Generator) -> np.ndarray:
    labels = np.asarray(labels)
    cols = []
    for s in np.unique(labels):
        idx = np.flatnonzero(labels == s)
        m = max(2, int(np.floor(frac * len(idx))))
        cols.append(rng.permuted(np.broadcast_to(idx, (B, len(idx))), axis=1)[:, :m])
    return np.sort(np.concatenate(cols, axis=1), axis=1)
