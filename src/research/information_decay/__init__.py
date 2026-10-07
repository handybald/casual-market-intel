"""M3 information-decay research layer (protocol: docs/research/information_decay_v1_methodology.md).

This package currently contains ONLY the synthetic estimator calibration (methodology §9-§10):
mutual-information estimators, permutation / bootstrap inference helpers, a replica of the frozen
M2 sign-conditioned baseline for simulation, scenario generators and the calibration runner.

REAL-DATA ISOLATION. Nothing in this package reads real surprise, response or residual values.
The package depends on numpy only -- it never imports pandas or pyarrow -- and the calibration
runner executes inside `isolation.real_data_guard()`, which refuses any file access under
data/processed, data/interim, data/raw or to any .parquet file. Real-data MI belongs to a later,
separately reviewed step that runs only after amendment 01 is committed.
"""
