"""Numpy-only special functions (the project environment has no scipy).

  ndtri(p)            inverse standard-normal CDF, Acklam's rational approximation refined by one
                      Halley step (|error| ~ 1e-15 on (0, 1)); vectorized
  digamma(x)          psi(x) for x > 0 via upward recurrence + asymptotic series; vectorized
  digamma_int_table   psi(1..m) from harmonic numbers (exact up to float rounding), for KSG
  wilson_interval     Wilson score interval for a binomial proportion
  NATS_PER_BIT        ln 2: bits = nats / ln 2
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Tuple

import numpy as np

NATS_PER_BIT = math.log(2.0)
EULER_GAMMA = 0.57721566490153286060651209


def nats_to_bits(x):
    return np.asarray(x, dtype=float) / NATS_PER_BIT


def bits_to_nats(x):
    return np.asarray(x, dtype=float) * NATS_PER_BIT


# --- inverse normal CDF -------------------------------------------------------------------------
_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
_P_LOW = 0.02425

_erfc = np.vectorize(math.erfc, otypes=[float])


def ndtri(p) -> np.ndarray:
    p = np.asarray(p, dtype=float)
    if np.any((p <= 0) | (p >= 1) | ~np.isfinite(p)):
        raise ValueError("ndtri requires 0 < p < 1")
    x = np.empty_like(p)
    lo = p < _P_LOW
    hi = p > 1 - _P_LOW
    mid = ~(lo | hi)
    if np.any(lo):
        q = np.sqrt(-2 * np.log(p[lo]))
        x[lo] = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
                ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    if np.any(hi):
        q = np.sqrt(-2 * np.log1p(-p[hi]))
        x[hi] = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
                 ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    if np.any(mid):
        q = p[mid] - 0.5
        r = q * q
        x[mid] = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
                 (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1)
    # one Halley refinement step using the exact complementary error function
    e = 0.5 * _erfc(-x / math.sqrt(2.0)) - p
    u = e * math.sqrt(2 * math.pi) * np.exp(x * x / 2)
    return x - u / (1 + x * u / 2)


@lru_cache(maxsize=None)
def normal_score_table(n: int) -> np.ndarray:
    """ndtri(j / (2 (n + 1))) for j = 0 .. 2n+2 (index j = 2 * rank): normal scores for integer
    and half-integer (average-tie) ranks 0.5 .. n. Entries at j=0 and j=2n+2 are unused (nan)."""
    j = np.arange(2 * n + 3, dtype=float)
    out = np.full(len(j), np.nan)
    inner = (j > 0) & (j < 2 * n + 2)
    out[inner] = ndtri(j[inner] / (2.0 * (n + 1)))
    out.setflags(write=False)
    return out


# --- digamma ------------------------------------------------------------------------------------
def digamma(x) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if np.any(x <= 0):
        raise ValueError("digamma implemented for x > 0 only")
    acc = np.zeros_like(x)
    y = x.copy()
    while True:
        small = y < 8
        if not np.any(small):
            break
        acc = acc - np.where(small, 1.0 / y, 0.0)
        y = np.where(small, y + 1, y)
    inv = 1.0 / y
    inv2 = inv * inv
    series = np.log(y) - 0.5 * inv - inv2 * (1 / 12 - inv2 * (1 / 120 - inv2 * (1 / 252 - inv2 * (1 / 240 - inv2 / 132))))
    return acc + series


@lru_cache(maxsize=None)
def digamma_int_table(m: int) -> np.ndarray:
    """psi(i) for i = 0..m (index 0 is nan): psi(i) = -gamma + H_{i-1}."""
    out = np.full(m + 1, np.nan)
    h = np.concatenate([[0.0], np.cumsum(1.0 / np.arange(1, m))])
    out[1:] = -EULER_GAMMA + h
    out.setflags(write=False)
    return out


# --- binomial interval --------------------------------------------------------------------------
def wilson_interval(k: int, n: int, z: float = 1.959963984540054) -> Tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))
