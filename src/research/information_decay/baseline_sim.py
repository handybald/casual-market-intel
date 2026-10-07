"""Array replica of the frozen M2 expected-response baseline, for SIMULATION ONLY.

Frozen rule (src/research/baseline.py, config/event_research.yaml `baseline`):
  expected_h(i) = mean of R_h over STRICTLY EARLIER releases of the same sign group
                  (group = sign(surprise_raw) in {-1, 0, +1}; a missing sign has no expectation)
  defined only if that history has >= min_history (8) releases; resid = R_h - expected_h.

Synthetic releases are equally spaced in time and every response is usable, so "strictly earlier"
is "earlier row" and the history count is the same for every horizon. A unit test proves this
replica reproduces src/research/baseline.add_baseline on synthetic frames; the frozen module is
never modified. The batched form recomputes the baseline for many permuted sign vectors at once
(pipeline-aware residual permutation, methodology §11.7, Null B).
"""
from __future__ import annotations

from typing import Tuple

import numpy as np

NO_SIGN = -9  # sentinel for a missing surprise sign
SIGN_GROUPS = (-1, 0, 1)


def expanding_sign_baseline(R: np.ndarray, sign: np.ndarray, min_history: int = 8) -> Tuple[np.ndarray, np.ndarray]:
    """R: (N, H) responses in time order; sign: (N,) in {-1, 0, 1, NO_SIGN}.
    Returns expected (N, H) (nan where undefined) and history count (N,)."""
    exp, cnt = expanding_sign_baseline_batch(R, np.asarray(sign)[None, :], min_history)
    return exp[0], cnt[0]


def expanding_sign_baseline_batch(R: np.ndarray, signs: np.ndarray, min_history: int = 8) -> Tuple[np.ndarray, np.ndarray]:
    """R: (N, H); signs: (B, N). Returns expected (B, N, H) and history counts (B, N)."""
    R = np.asarray(R, dtype=float)
    signs = np.asarray(signs)
    B, N = signs.shape
    H = R.shape[1]
    expected = np.full((B, N, H), np.nan)
    count = np.zeros((B, N), dtype=int)
    for g in SIGN_GROUPS:
        m = signs == g
        if not m.any():
            continue
        mf = m.astype(float)
        c_incl = np.cumsum(mf, axis=1)
        c_excl = c_incl - mf
        s_excl = np.cumsum(mf[:, :, None] * R[None, :, :], axis=1) - mf[:, :, None] * R[None, :, :]
        ok = m & (c_excl >= min_history)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = s_excl / c_excl[:, :, None]
        expected = np.where(ok[:, :, None], mean, expected)
        count = np.where(m, c_excl.astype(int), count)
    return expected, count
