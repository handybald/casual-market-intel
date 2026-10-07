"""Transparent benchmarks (methodology §8.3-§8.5). Never primary nonlinear MI estimators.

  gaussian_mi_bits(rho)   I = -1/2 log2(1 - rho^2): exact MI of a bivariate Gaussian; applied to a
                          sample Pearson r it is the linear benchmark I_lin
  pearson, spearman       correlation benchmarks (spearman = pearson on average ranks)
  histogram_mi_bits       plug-in MI on an equal-frequency b x b grid (default b = 4, fixed a priori);
                          CALIBRATION-ONLY illustration of small-n binning sensitivity
"""
from __future__ import annotations

import numpy as np

from .gcmi import _corr_rows, average_ranks, gaussian_mi_bits_from_r


def gaussian_mi_bits(rho) -> np.ndarray:
    return gaussian_mi_bits_from_r(rho)


def pearson(x, y) -> float:
    return float(_corr_rows(np.asarray(x, float), np.asarray(y, float)))


def spearman(x, y) -> float:
    return float(_corr_rows(average_ranks(x), average_ranks(y)))


def equal_frequency_bins(x, b: int) -> np.ndarray:
    """Bin index 0..b-1 from ordinal position (stable sort); equal counts up to rounding."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    order = np.argsort(x, kind="mergesort")
    out = np.empty(n, dtype=int)
    out[order] = (np.arange(n) * b) // n
    return out


def histogram_mi_bits(x, y, b: int = 4) -> float:
    bx, by = equal_frequency_bins(x, b), equal_frequency_bins(y, b)
    n = len(bx)
    joint = np.zeros((b, b))
    np.add.at(joint, (bx, by), 1.0)
    pxy = joint / n
    px = pxy.sum(axis=1, keepdims=True)
    py = pxy.sum(axis=0, keepdims=True)
    nz = pxy > 0
    return float((pxy[nz] * np.log2(pxy[nz] / (px @ py)[nz])).sum())
