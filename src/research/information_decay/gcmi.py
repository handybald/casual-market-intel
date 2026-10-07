"""Bivariate Gaussian-copula mutual information (GCMI; Ince et al. 2017, Hum. Brain Mapp.).

  1. rank-transform each marginal          (tie policy explicit -- see below)
  2. Gaussianize: z = Phi^{-1}(rank / (n + 1))   ("copula normalization", explicit)
  3. I_GC = -1/2 log2(1 - r^2),  r = Pearson correlation of the two normal-score vectors

Returned in BITS. Fully deterministic: no randomness anywhere in this module (seeded random tie
breaking, when a policy asks for it, is applied by the caller with an explicit generator).

SCOPE. For scalar S and scalar R this is a measure of Gaussian-copula, essentially monotonic,
dependence. It is a LOWER BOUND on the true MI under the Gaussian-copula assumption and is close to
zero for non-monotone (U-shaped) or purely heteroskedastic dependence. It is NOT a universal
nonlinear MI estimator.

TIES. `average_ranks` gives tied values their average (possibly half-integer) rank, so tied inputs
receive identical normal scores (deterministic; no ordering artefact). This differs from Ince's
reference toolbox, whose argsort-based ranks break ties by input order -- with time-ordered data,
by time. Other tie policies (methodology D3) are built from these primitives in calibration.py.

BIAS. `mi_observed` is the plain plug-in value. The closed-form Gaussian-entropy bias correction
of Ince et al. (`ince_bias_bits`) is provided as a diagnostic only; the primary correction is the
permutation null (MI_effective = MI_observed - mean(null)), never stacked with this one.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from .special import NATS_PER_BIT, digamma, normal_score_table


def average_ranks(x) -> np.ndarray:
    """1-based ranks; ties get the average rank. Deterministic."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    new = np.empty(n, dtype=bool)
    new[:1] = True
    new[1:] = xs[1:] != xs[:-1]
    starts = np.flatnonzero(new)
    counts = np.diff(np.append(starts, n))
    avg = starts + (counts + 1) / 2.0
    r = np.empty(n)
    r[order] = np.repeat(avg, counts)
    return r


def average_ranks_2d(x: np.ndarray, valid: np.ndarray = None) -> np.ndarray:
    """Row-wise average ranks of a (B, N) array. Entries with valid == False are excluded (their
    rank is 0) and the ranks of valid entries run 1..n_valid(row)."""
    x = np.asarray(x, dtype=float)
    B, N = x.shape
    if valid is None:
        valid = np.ones_like(x, dtype=bool)
    xm = np.where(valid, x, np.inf)
    order = np.argsort(xm, axis=1, kind="mergesort")
    xs = np.take_along_axis(xm, order, axis=1)
    vs = np.take_along_axis(valid, order, axis=1)
    pos = np.broadcast_to(np.arange(N), (B, N))
    new = np.ones((B, N), dtype=bool)
    new[:, 1:] = (xs[:, 1:] != xs[:, :-1]) | (vs[:, 1:] != vs[:, :-1])
    start = np.maximum.accumulate(np.where(new, pos, 0), axis=1)
    end_flag = np.ones((B, N), dtype=bool)
    end_flag[:, :-1] = new[:, 1:]
    end = np.minimum.accumulate(np.where(end_flag, pos, N)[:, ::-1], axis=1)[:, ::-1]
    avg = np.where(vs, (start + end) / 2.0 + 1.0, 0.0)
    out = np.empty((B, N))
    np.put_along_axis(out, order, avg, axis=1)
    return out


@lru_cache(maxsize=None)
def _score_matrix(n_max: int) -> np.ndarray:
    """T[n, 2*rank] = Phi^{-1}(rank / (n + 1)) for n <= n_max (half-integer ranks supported)."""
    T = np.zeros((n_max + 1, 2 * n_max + 3))
    for n in range(1, n_max + 1):
        T[n, : 2 * n + 3] = np.nan_to_num(normal_score_table(n))
    T.setflags(write=False)
    return T


def scores_from_ranks(ranks, n) -> np.ndarray:
    """Normal scores Phi^{-1}(rank/(n+1)); `ranks` 1-based, integer or half-integer."""
    ranks = np.asarray(ranks, dtype=float)
    idx = np.rint(2 * ranks).astype(int)
    if np.ndim(n) == 0:
        return normal_score_table(int(n))[idx]
    n = np.asarray(n, dtype=int)
    T = _score_matrix(int(max(256, n.max())))
    return T[n.reshape(n.shape + (1,) * (idx.ndim - n.ndim)), idx]


def copnorm(x) -> np.ndarray:
    """Copula normalization with average-rank ties."""
    x = np.asarray(x, dtype=float)
    return scores_from_ranks(average_ranks(x), len(x))


def gaussian_mi_bits_from_r(r) -> np.ndarray:
    r = np.clip(np.asarray(r, dtype=float), -0.999999999999, 0.999999999999)
    return -0.5 * np.log1p(-r * r) / NATS_PER_BIT


def _corr_rows(a: np.ndarray, b: np.ndarray, mask: np.ndarray = None) -> np.ndarray:
    """Pearson correlation along the last axis (broadcasting); optional validity mask."""
    if mask is None:
        a = a - a.mean(axis=-1, keepdims=True)
        b = b - b.mean(axis=-1, keepdims=True)
    else:
        cnt = mask.sum(axis=-1, keepdims=True)
        a = np.where(mask, a - (np.where(mask, a, 0).sum(-1, keepdims=True) / cnt), 0)
        b = np.where(mask, b - (np.where(mask, b, 0).sum(-1, keepdims=True) / cnt), 0)
    num = (a * b).sum(axis=-1)
    den = np.sqrt((a * a).sum(axis=-1) * (b * b).sum(axis=-1))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, 0.0)


def gcmi_bits(x, y) -> float:
    """GCMI (bits) between two 1-D samples, average-rank ties."""
    return float(gaussian_mi_bits_from_r(_corr_rows(copnorm(x), copnorm(y))))


def gcmi_from_scores(zs: np.ndarray, zy: np.ndarray, mask: np.ndarray = None) -> np.ndarray:
    """Vectorized GCMI from precomputed normal scores. zs: (..., n), zy: (..., n) broadcastable."""
    return gaussian_mi_bits_from_r(_corr_rows(zs, zy, mask))


def gcmi_permutation_matrix(zs: np.ndarray, zy: np.ndarray, perms: np.ndarray) -> np.ndarray:
    """GCMI for every permutation of the surprise scores against each response column.
    zs: (n,), zy: (n, H), perms: (B, n) index arrays -> (B, H) bits. Uses one matrix product."""
    zs_c = zs - zs.mean()
    zy_c = zy - zy.mean(axis=0)
    with np.errstate(all="ignore"):  # numpy 2.0 + Accelerate raises spurious matmul FP flags
        num = zs_c[perms] @ zy_c
    den = np.sqrt((zs_c * zs_c).sum() * (zy_c * zy_c).sum(axis=0))
    return gaussian_mi_bits_from_r(num / den)


def ince_bias_bits(n: int) -> float:
    """Closed-form Gaussian-entropy bias of the 1-D x 1-D Gaussian MI (Ince et al. 2017), bits.
    Diagnostic only: mi_bias_corrected = mi_observed - ince_bias_bits(n)."""
    d = digamma(np.array([(n - 1) / 2.0, (n - 2) / 2.0]))
    return float((d[0] - d[1]) / 2.0 / NATS_PER_BIT)
