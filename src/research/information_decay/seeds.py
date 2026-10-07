"""Deterministic, loop-order-independent seed derivation (methodology §17).

  key  = "m3_information_decay|v1|" + "|".join(parts)
  seed = first 8 bytes (big-endian) of SHA-256(key)
  rng  = numpy Generator(PCG64(SeedSequence(seed)))

Every stochastic procedure builds its own generator from stable identifiers (procedure, scenario,
n, replication, estimator, ...). There is no global RNG stream, so results never depend on loop
order, chunking or the number of worker processes.
"""
from __future__ import annotations

import hashlib

import numpy as np

KEY_PREFIX = "m3_information_decay|v1"


def seed_key(*parts) -> str:
    return "|".join([KEY_PREFIX, *[str(p) for p in parts]])


def derive_seed(*parts) -> int:
    return int.from_bytes(hashlib.sha256(seed_key(*parts).encode("utf-8")).digest()[:8], "big")


def rng_for(*parts) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(derive_seed(*parts))))
