"""Kraskov-Stoegbauer-Grassberger mutual information, algorithm 1 (Phys. Rev. E 69, 066138, 2004).

  eps_i  = distance from point i to its k-th nearest neighbour in the joint space (max-norm)
  n_x(i) = #{j != i : |x_i - x_j| < eps_i},  n_y(i) likewise
  I      = psi(k) + psi(N) - < psi(n_x + 1) + psi(n_y + 1) >        (nats -> returned in BITS)

ASSUMPTIONS. Continuous variables with no exact ties. Callers z-score both variables (unit
variance) and, per policy, add seeded jitter of fixed tiny amplitude (1e-10 in z units) to break
exact ties deterministically. With exact duplicates (e.g. bootstrap resamples) eps_i can be 0;
then n_x = n_y = 0 for that point (strict inequality) -- the estimator's known degeneracy, which
the calibration measures rather than hides.

k is a robustness family {3, 5, 10}; it is never tuned on real data. Deterministic given inputs.
Implementation: dense pairwise distance matrices, vectorized over a leading batch axis (used for
permutation, bootstrap and subsampling batches); n <= a few hundred.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .special import NATS_PER_BIT, digamma_int_table

DEFAULT_KS = (3, 5, 10)


def zscore(x, axis=-1, mask=None) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if mask is None:
        mu = x.mean(axis=axis, keepdims=True)
        sd = x.std(axis=axis, ddof=1, keepdims=True)
    else:
        cnt = mask.sum(axis=axis, keepdims=True)
        mu = np.where(mask, x, 0).sum(axis=axis, keepdims=True) / cnt
        dev = np.where(mask, x - mu, 0)
        sd = np.sqrt((dev * dev).sum(axis=axis, keepdims=True) / np.maximum(cnt - 1, 1))
    sd = np.where(sd > 0, sd, 1.0)
    return (x - mu) / sd


def ksg_batch_bits(x: np.ndarray, y: np.ndarray, ks: Sequence[int] = DEFAULT_KS,
                   valid: np.ndarray = None, chunk: int = 64) -> np.ndarray:
    """KSG (bits) for a batch. x, y: (B, n) or (n,) (broadcast). valid: optional (B, n) mask of
    points that belong to each batch member's sample. Returns (B, len(ks))."""
    x = np.atleast_2d(np.asarray(x, dtype=float))
    y = np.atleast_2d(np.asarray(y, dtype=float))
    B = max(x.shape[0], y.shape[0])
    n = x.shape[1]
    ks = [int(k) for k in ks]
    if valid is not None:
        valid = np.broadcast_to(np.atleast_2d(valid), (B, n))
    psi = digamma_int_table(n + 2)
    out = np.empty((B, len(ks)))
    eye = np.eye(n, dtype=bool)
    dy_fixed = None
    if y.shape[0] == 1:
        dy_fixed = np.abs(y[0][:, None] - y[0][None, :])
    for s in range(0, B, chunk):
        e = min(B, s + chunk)
        xb = x[s:e] if x.shape[0] > 1 else np.broadcast_to(x, (e - s, n))
        dx = np.abs(xb[:, :, None] - xb[:, None, :])
        dy = (np.broadcast_to(dy_fixed, (e - s, n, n)) if dy_fixed is not None
              else np.abs(y[s:e][:, :, None] - y[s:e][:, None, :]))
        excl = np.broadcast_to(eye, (e - s, n, n))
        if valid is not None:
            vb = valid[s:e]
            excl = excl | ~(vb[:, :, None] & vb[:, None, :])
        dx = np.where(excl, np.inf, dx)
        dy = np.where(excl, np.inf, dy)
        d = np.maximum(dx, dy)
        kth = sorted(set(k - 1 for k in ks))
        part = np.partition(d, kth, axis=2)
        if valid is not None:
            nv = vb.sum(axis=1)
            wts = vb / nv[:, None]
        else:
            nv = np.full(e - s, n)
            wts = np.full((e - s, n), 1.0 / n)
        for j, k in enumerate(ks):
            eps = part[:, :, k - 1][:, :, None]
            nx = (dx < eps).sum(axis=2)
            ny = (dy < eps).sum(axis=2)
            term = (psi[nx + 1] + psi[ny + 1]) * wts
            out[s:e, j] = psi[k] + psi[nv] - term.sum(axis=1)
    return out / NATS_PER_BIT


def ksg_bits(x, y, ks: Sequence[int] = DEFAULT_KS) -> np.ndarray:
    """KSG (bits) for one sample, after z-scoring both variables. Returns (len(ks),)."""
    return ksg_batch_bits(zscore(x)[None, :], zscore(y)[None, :], ks)[0]
