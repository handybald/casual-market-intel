"""True mutual information of synthetic scenarios by deterministic numerical integration.

Model: S ~ N(0, 1),  R = g(S) + sigma(S) * eps,  eps ~ f (standard normal or standard Student t_nu)

  I(S; R) = h(R) - h(R | S),   h(R | S) = h(eps) + E[ln sigma(S)]
  p_R(r)  = E_S[ f((r - g(S)) / sigma(S)) / sigma(S) ]

S is integrated on a uniform grid over [-9, 9] (normal weights renormalized); r on a sinh-stretched
grid that reaches |r| ~ 2e4, so heavy (t_3) tails are covered. h(eps) is closed form. Results in
bits. The Gaussian case g = rho*s, sigma = sqrt(1 - rho^2) reproduces -1/2 log2(1 - rho^2) to
< 1e-6 bits (unit-tested). `solve_strength` finds the strength parameter giving a target MI by
bisection (MI is monotone in the strength for every scenario that uses it).
"""
from __future__ import annotations

import math
from typing import Callable, Dict, Optional

import numpy as np

from .special import NATS_PER_BIT, digamma


def _noise_logpdf(z: np.ndarray, nu: Optional[float]) -> np.ndarray:
    if nu is None:
        return -0.5 * z * z - 0.5 * math.log(2 * math.pi)
    c = math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) - 0.5 * math.log(nu * math.pi)
    return c - (nu + 1) / 2 * np.log1p(z * z / nu)


def noise_entropy_nats(nu: Optional[float]) -> float:
    if nu is None:
        return 0.5 * math.log(2 * math.pi * math.e)
    psi = digamma(np.array([(nu + 1) / 2, nu / 2]))
    lbeta = math.lgamma(nu / 2) + math.lgamma(0.5) - math.lgamma((nu + 1) / 2)
    return float((nu + 1) / 2 * (psi[0] - psi[1]) + 0.5 * math.log(nu) + lbeta)


def true_mi_bits(g: Callable[[np.ndarray], np.ndarray], sigma: Callable[[np.ndarray], np.ndarray],
                 nu: Optional[float] = None, n_s: int = 1801, n_u: int = 24001,
                 r_max: float = 2.0e4, chunk: int = 1500) -> float:
    s = np.linspace(-9.0, 9.0, n_s)
    w = np.exp(-0.5 * s * s)
    w /= w.sum()
    gs = np.asarray(g(s), dtype=float) * np.ones_like(s)
    ss = np.asarray(sigma(s), dtype=float) * np.ones_like(s)
    u = np.linspace(-math.asinh(r_max), math.asinh(r_max), n_u)
    r = np.sinh(u)
    dr = np.cosh(u) * (u[1] - u[0])
    p = np.empty(n_u)
    for a in range(0, n_u, chunk):
        b = min(n_u, a + chunk)
        z = (r[None, a:b] - gs[:, None]) / ss[:, None]
        dens = np.exp(_noise_logpdf(z, nu)) / ss[:, None]
        with np.errstate(all="ignore"):  # numpy 2.0 + Accelerate raises spurious matmul FP flags
            p[a:b] = w @ dens
    mass = float((p * dr).sum())
    if abs(mass - 1) > 1e-6:
        raise RuntimeError(f"numerical density does not integrate to 1 (mass {mass})")
    pos = p > 0
    h_r = float(-(p[pos] * np.log(p[pos]) * dr[pos]).sum())
    h_r_given_s = noise_entropy_nats(nu) + float((w * np.log(ss)).sum())
    return (h_r - h_r_given_s) / NATS_PER_BIT


def scenario_model(name: str, beta: float) -> Dict:
    """(g, sigma, nu) of the single-horizon marginal model of each strength-calibrated scenario."""
    if name == "monotone_tanh":
        return {"g": lambda s: np.tanh(beta * s), "sigma": lambda s: 1.0, "nu": None}
    if name == "u_shape":
        return {"g": lambda s: beta * (s * s - 1.0), "sigma": lambda s: 1.0, "nu": None}
    if name == "heteroskedastic":
        return {"g": lambda s: 0.0, "sigma": lambda s: 1.0 + beta * np.abs(s), "nu": None}
    if name == "heavy_tail_t3":
        return {"g": lambda s: beta * s, "sigma": lambda s: 1.0, "nu": 3.0}
    if name == "gaussian":
        return {"g": lambda s: beta * s, "sigma": lambda s: math.sqrt(1 - beta * beta), "nu": None}
    raise KeyError(name)


def scenario_true_mi_bits(name: str, beta: float) -> float:
    m = scenario_model(name, beta)
    return true_mi_bits(m["g"], m["sigma"], m["nu"])


def solve_strength(name: str, target_bits: float, lo: float = 0.0, hi: float = 10.0,
                   tol: float = 1e-7, max_iter: int = 200) -> float:
    f_hi = scenario_true_mi_bits(name, hi)
    if f_hi < target_bits:
        raise ValueError(f"{name}: target {target_bits} not reachable at strength {hi} ({f_hi})")
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if scenario_true_mi_bits(name, mid) < target_bits:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)
