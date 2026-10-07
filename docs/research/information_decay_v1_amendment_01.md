# M3 Information Decay v1 — Amendment 01: Synthetic Estimator Calibration Outcome

| Field | Value |
|---|---|
| Amends | `docs/research/information_decay_v1_methodology.md`, frozen at `d976c3e408ec770ffa6633af22b422d3814e0e16` (blob `f6199450ec23de9dca9b6e7f4f28260cae5cec25`, unchanged since freeze) |
| Real-data MI computed when this amendment was written | **NO** |
| Calibration config | `config/information_decay_v1.yaml` — sha256 `bd877f7dfd6d6bb20a6fdd78ba92f40cefb17bbaa5775329eedafde6a70aa5c3` (canonical-JSON sha256 `f5d6a8b9c0e178477b1ad8e54733b58728aaee20e05ba6c32d6bfcba3caf27b8`) |
| Implementation | HEAD `d976c3e`, uncommitted calibration code: `uncommitted_changes_sha256 = 5e061aa4d89f43431b764e38c96084f598ec391b22ebd334179a9fb62e1e44ad`, re-verified against the working tree after the run |
| Outputs | `data/reports/information_decay/calibration_v1/` — combined sha256 `c451c13fb1cee3c413e575f60ff5cbdcdf54a7c9f48150f964135351b6b8fbfc`; per-file hashes in `metadata/research/information_decay_calibration_v1.json` |
| Run | profile `final`, 160 (cell × n) tasks, 8 workers, wall time 13,475 s (2026-10-05T21:30Z start) |

## 0. Outcome in one paragraph

The pre-specified decision rules were applied mechanically by
`src/research/information_decay/decisions.py`. **D1 is not satisfied: no
candidate estimator passed all of the validity criteria V1–V4.**

- GCMI failed **V4** (outlier robustness).
- KSG k = 3, 5 and 10 failed **V3** (bias / lower-bound integrity). k = 5
  also failed V1 at n = 60.
- Every V3/V4 failure comes from a single scenario cell,
  `s7_alt_contaminated`.

Methodology §10.3 says that when no estimator qualifies, *"real-data MI is
stopped pending revision"*. **Real-data MI execution is therefore BLOCKED.**
The rule chain stops at D1, so D2 and D4–D8 were **not decided**. D3 does not
depend on D1 and **was decided**. The evidence for D2 and D4–D8 is recorded
below, clearly labelled as evidence or counterfactual, to support the
required methodology revision. **None of it is adopted as a decision here.**

## 1. Pre-final-run changes and errata (all made before any final output existed)

1. **`I_ref` erratum.** The methodology states `I_ref ≈ 0.098 bits` and
   `≈ 0.0976`. The defining expression `−½·log2(1 − 0.35²)` equals
   **0.094264 bits**. The definition (ρ = 0.35) governs, and the calibration
   uses 0.094264.
2. **D8 operationalization.** The frozen text, "per-horizon KSG null means
   differ by more than 20%", is undefined for KSG, whose null means are about
   0 and can be negative. It was fixed in the config, before the final run,
   as: studentize if `(max_h − min_h null mean) > 0.20 × mean null SD` or
   `max/min null SD > 1.20`, for any k at grid n ≥ 60.
3. **KSG replicate count.** A timing-only benchmark projected about 55 min at
   1,000 KSG replicates and about 1.8 h at 2,000. 1,500 was chosen as the
   largest count inside the ~2 h budget. All other cells use 2,000 Monte Carlo
   replicates.
   - **Runtime overrun:** the real wall time was 3.7 h. Contention among 8
     concurrent workers ran about 2.3× slower than the single-process
     benchmark.
   - **Aborted first run:** a first `final` run was killed after about 23 h
     without producing any output. The parent process was buffering
     per-replicate arrays and swapping.
   - **Restart:** the runner was restructured to aggregate per (cell, n)
     inside the workers. Before the restart, a test-profile run confirmed the
     restructure is **byte-identical** to the previous runner's CSVs. No
     estimator output was seen before the restart.
4. **Cell key renamed** `null` → `is_null`, because YAML parses the key
   `null` as `None`. This is cosmetic.

## 2. Simulation design as run

- **Sample sizes:** grid n ∈ {40, 60, 80, 100, 120}, plus the real cohort
  sizes {56, 74, 77, 97} for cells that matter for D2.
- **Horizons:** {1, 5, 15, 30, 60}, with cumulative Brownian noise:
  `corr = √(min/max)`.
- **Strata:** 0.30 / 0.25 / 0.45, with the merge rule (minimum 8 per
  stratum).
- **GCMI, correlations, `I_lin`, histogram MI:** 2,000 Monte Carlo replicates
  per cell. Each replicate uses 1,000 stratified permutations. Coverage cells
  add 500 bootstrap draws and 500 subsamples.
- **KSG, k ∈ {3, 5, 10}:** the first 1,500 replicates. Each uses the first
  200 of the *same* permutations. Coverage uses 500 replicates × 200
  bootstrap draws or subsamples.
- **Scenarios:** 24 cells implementing scenarios 1, 1h and 2–10 of
  methodology §9.2. Non-Gaussian strengths were solved numerically so that
  true MI = `I_ref`:

  | Scenario | Strength parameter |
  |---|---|
  | tanh | β = 0.43196 |
  | U-shape | β = 0.26771 |
  | heteroskedastic | β = 0.77750 |
  | t₃ | β = 0.46608 |

  The truth engine reproduces the analytic Gaussian MI to under 1e-6 bits.
- **Seeds:** `sha256("m3_information_decay|v1|calibration|<cell>|<n>|<rep>|<purpose>")[:8]`
  feeding PCG64. Results are byte-identical across worker counts, and this is
  tested.
- **M2 baseline replica:** the frozen sign-conditioned baseline replica is
  proven equal to `src/research/baseline.add_baseline` by a test.
- **Type-I acceptance:** a rate is acceptable if it is
  `≤ 0.05 + 2·√(0.05·0.95/R)`. That is 0.0597 for R = 2,000 and 0.0613 for
  R = 1,500.

## 3. Results

### 3.1 Type-I error

Values are horizon-adjusted panel rates for stratified permutation, the range
over all n.

| Null cell | GCMI | KSG k=5 |
|---|---|---|
| s1 independence (mode A) | 0.042–0.059 | 0.044–**0.067** (n = 60) |
| s1 independence (mode B) | 0.045–0.054 | 0.045–0.056 |
| s6 heavy-tail null | 0.039–0.053 | 0.044–0.057 |
| s10 regime null, **stratified** | 0.040–0.057 | 0.042–0.058 |
| s10 regime null, **unrestricted** | **0.121–0.284** | **0.079–0.105** |

- Stratified release-level permutation with max-T controls type-I error for
  both estimators.
- Unrestricted permutation is badly anti-conservative under regime shifts.
  This confirms the frozen choice of stratified permutation.
- The single KSG exceedance, 0.067 > 0.0613, is at Monte Carlo-noise scale.

### 3.2 Bias, effective MI, RMSE

These are pooled over horizons; truth at ρ = 0.35 is 0.0943 bits.

| Statistic | n | GCMI | KSG k=3 | KSG k=5 | KSG k=10 |
|---|---|---|---|---|---|
| Null raw MI | 40 → 120 | 0.0188 → 0.0062 | ≈ 0 | ≈ 0 | ≈ 0 |
| Null `MI_eff` | 40 → 120 | ≈ 0 (\|·\| ≤ 0.0004) | ≈ 0 | ≈ 0 | ≈ 0 |
| `MI_eff`, Gaussian ρ = 0.35 | 60 / 120 | 0.0908 / 0.0910 | 0.0935 / 0.0954 | 0.0892 / 0.0934 | 0.0772 / 0.0870 |
| SD of `MI_eff`, ρ = 0.35 | 60 / 120 | 0.067 / 0.047 | — | 0.102 / 0.078 | 0.075 / 0.060 |
| `MI_eff`, copula s3a | 120 | 0.0920 | 0.056 | 0.048 | 0.038 |

- Subtracting the permutation null works as intended. Under the null,
  `MI_eff` is centred at 0 for every estimator.
- GCMI behaves as a slight lower bound, about 3–4% low.
- KSG is close to unbiased for Gaussian data but **not invariant to monotone
  marginal transforms in finite samples**. In scenario 3a it recovers only
  40–60% of the true MI.
- The histogram estimator (b = 4) is strongly biased upward, at 0.19 bits
  under the null at n = 40. It stays a calibration-only illustration.

### 3.3 Power

These are horizon-adjusted panel rejection rates.

| Scenario | GCMI n = 60 / 77 / 97 | KSG k=10 n = 60 / 77 / 97 | KSG k=5 n = 60 / 77 / 97 |
|---|---|---|---|
| Gaussian ρ = 0.35 (mode A) | **0.902 / 0.959 / 0.991** | 0.579 / 0.648 / 0.723 | 0.402 / 0.444 / 0.515 |
| Gaussian ρ = 0.25 (grid n = 60 / 80 / 100) | 0.616 / 0.767 / 0.834 | — | 0.201 / 0.227 / 0.239 |
| Monotone tanh (n = 60 / 100) | 0.894 / 0.990 | — | 0.413 / 0.546 |
| U-shape (n = 60 / 100) | **0.085 / 0.069** | — | 0.215 / 0.321 |
| Heteroskedastic (n = 60 / 100) | 0.132 / 0.148 | — | 0.153 / 0.243 |
| Discrete CPI-like, s9 alternative (n = 60 / 100) | 0.855 / 0.982 | — | 0.313 / 0.446 |

- GCMI dominates for monotone dependence.
- As expected a priori, GCMI is blind to U-shaped dependence. KSG detects it,
  though weakly.
- Pointwise GCMI power at ρ = 0.35 is 0.78 at n = 60 and 0.94 at n = 97.

### 3.4 Residual null (scenario 8): the critical D4 evidence

These are horizon-adjusted type-I rates, the range over all n.

| s8 null variant | GCMI Null A | GCMI Null B | KSG k=5 Null A | KSG k=5 Null B | matched raw (GCMI) |
|---|---|---|---|---|---|
| mode A, Gaussian noise | 0.040–0.054 | 0.037–0.057 | 0.042–0.055 | 0.039–0.057 | 0.049–0.063 |
| mode B continuous | 0.044–0.056 | 0.046–0.055 | 0.043–0.058 | 0.045–0.059 | 0.041–0.057 |
| mode B discrete (CPI-like) | 0.045–0.063 | 0.042–0.059 | 0.037–0.055 | 0.040–0.058 | 0.043–0.062 |
| **heavy-tailed t₃ responses** | **0.099–0.124** | 0.041–0.055 | **0.089–0.109** | 0.042–0.060 | 0.037–0.060 |

**Fixed-residual permutation (Null A) does NOT control type-I error** once
responses are heavy-tailed. Its rate roughly doubles, to about 0.10–0.12. The
cause is that a few large shocks shift a sign group's expanding mean for a
long time, and that creates mechanical S–E dependence (methodology §2.1).
**Pipeline-aware permutation (Null B) is calibrated in every variant.** Market
returns are heavy-tailed, so this is the relevant case.

**Residual power is low by construction.** Generate raw responses with
ρ = 0.35, then residualize with the frozen sign-conditioned baseline. Panel
power on the residual is:

- GCMI with Null B: 0.22 at n = 60, 0.32 at n = 100;
- matched raw on the same releases: 0.90 / 0.99.

This confirms the analytic attenuation in methodology §2.1(3): residual MI
measures dependence beyond the historical sign-mean response, not the main
linear signal.

### 3.5 Tie policy (scenario 9: discrete surprise, trending responses)

| Estimator / policy | Horizon-adjusted null rate (all n) | Grid n ≥ 60 all within acceptance? |
|---|---|---|
| GCMI A (average ranks of `surprise_std`) | 0.043–0.060 | **no**: 0.0600 > 0.0597 at n = 100 |
| GCMI B (raw-group average ranks) | 0.044–0.062 | yes; but n = 77 gives 0.062, outside acceptance |
| GCMI C (raw groups, seeded within-group order) | 0.040–0.056 | yes |
| KSG K-A / K-B | k = 5: 0.043–0.063 (K-A), 0.041–0.056 (K-B) | yes, for every k ∈ {3, 5, 10} |

No policy shows a material artefact. All exceedances are within Monte Carlo
noise of nominal.

### 3.6 Interval coverage

Coverage is that of the true MI by the `MI_eff` interval.

| Method | GCMI: minimum over ρ ≥ 0.25, n ≥ 60 | GCMI: mean \|cov − 0.95\| | KSG k=5 |
|---|---|---|---|
| stratified percentile bootstrap | **0.928** | **0.020** | collapses with n: 0.73 → 0.23 at ρ = 0.35 (duplicate-tie degeneracy) |
| bias-recentred bootstrap | 0.910 | 0.029 | ≈ 1.00 (grossly conservative) |
| m-out-of-n subsampling (√(m/n) rescale) | 0.584 | 0.323 | 0.53–0.71 |

As the methodology anticipated (§12.2–12.3), the ordinary bootstrap is invalid
for KSG. The subsampling rescale under-covers for both estimators. MI is
non-regular near zero and the √n rate assumption fails.

### 3.7 KSG null balance (scenario 1h, heterogeneous horizon margins)

- The spread of per-horizon null means is at most 0.009 of the null SD.
- The SD ratio is at most 1.05.
- Studentized and raw max-T give the same type-I rate.

## 4. Decisions

| # | Decision | Status |
|---|---|---|
| **D1** | Primary estimator | **NOT SATISFIED: no estimator passes V1–V4 → real-data MI BLOCKED pending a methodology revision** (§10.3) |
| D2 | Minimum n | Not reached (depends on D1). Evidence in §5. |
| **D3** | Tie policy | **DECIDED.** GCMI: **policy B**, because A narrowly failed scenario-9 type-I at n = 100 and B passes at all grid n ≥ 60. KSG: **K-A**. |
| D4 | Residual primacy and null | Not reached (depends on D1). The evidence is unambiguous for *both* estimators: Null A fails, Null B is calibrated (§3.4). |
| D5 | Primary interval method | Not reached (depends on D1). Evidence in §5. |
| D6 | Standardization mode | Not reached (depends on D1). Evidence in §5. |
| D7 | KSG uncertainty | Not reached formally. Evidence: no KSG interval method is calibrated (§3.6). |
| D8 | KSG studentization | Not reached formally. Evidence: no imbalance (§3.7). |

### Why D1 failed

The diagnosis was verified against the code and the frozen scenario
specification. These are not implementation errors.

- **GCMI fails V4 only in `s7_alt_contaminated`.** There, 1–2 outliers
  placed *against* a ρ = 0.35 dependence shift GCMI by a median of 0.053–0.070
  bits, against 0.027–0.037 for KSG k = 5. In the other contamination cell
  (`s7_null_contaminated`), GCMI is about 3× *more* robust than KSG, at
  0.010–0.016 vs 0.029–0.042 bits. Rank transformation puts an adversarial
  outlier at extreme normal scores, while a far-away point barely changes
  KSG's neighbour counts.
- **KSG fails V3 only in `s7_alt_contaminated`.** The contaminated sample has
  mean `MI_eff` of 0.108–0.117 bits against the *clean* truth of 0.094. KSG
  reads the isolated outlier cluster as information.

## 5. Counterfactual diagnostic (NOT a decision)

To inform the revision, the same rule code was run with GCMI forced as
primary. This was done from a scratch copy, so the artifact-producing code
was not modified. It reproduces the stored D1 and D3 exactly. If GCMI were
primary:

- **D4:** residual MI stays primary, with **Null B (pipeline-aware)**.
- **D2:** `n_min` = **60**. Panel power is 0.902 / 0.890 in modes A / B, and
  the bias of `MI_eff` is within 20% of `I_ref`. Panel eligibility at the
  exact sizes:
  - SPY NFP (97): pass.
  - QQQ CPI and Core CPI (56): pass.
  - QQQ NFP (74): pass.
  - **SPY CPI and Core CPI (77): FAIL, through V2.** Policy B's scenario-9
    null rate at n = 77 is 0.062 > 0.0597.
- **D5:** stratified percentile bootstrap, `calibrated`.
- **D6:** mode B canonical. It is flagged "material" only at n = 80, where
  the `MI_eff` difference of 0.0044 bits exceeds 2 combined MC SE. Power
  differences are ≤ 0.012.
- **D7:** no formal KSG CI.
- **D8:** raw-MI max-statistic.

## 6. Issues the methodology revision must resolve

This amendment does not resolve them. They are for the project owner and
independent review.

1. **Scenario 7 design versus V3/V4.**
   - V3 compares estimates on contaminated data to the *clean* MI, but the
     contaminated distribution genuinely has different MI.
   - V4 compares absolute paired shifts across estimators with different
     sampling variance and different failure directions.
   - Every D1 failure traces to this one cell.
   - Options: keep V4 and accept "no qualified estimator"; redefine
     robustness, for example as a relative shift, or as rejection behaviour
     under contamination; or add a robustness pre-filter such as rank-based
     influence diagnostics.
   - Any change is selection after observing *synthetic* results and must be
     justified on methodological grounds.
2. **Multiplicity of acceptance checks.** Each estimator faces about 20–40
   per-cell checks at a 2·MC SE threshold, which is about a 2.3% exceedance
   chance per check even when calibration is perfect. Several "failures"
   (KSG k = 5 at n = 60; GCMI policy A at n = 100; policy B at n = 77; D6 at
   n = 80) are of this kind. A revision should consider a pooled or
   familywise acceptance criterion, for example a binomial test over cells or
   a Holm-style acceptance.
3. **Residual inference.** If residual MI stays primary, Null B is required.
   Its low power for linear signals (§3.4) should be made explicit in the
   primary hypothesis, or matched raw MI should be reconsidered as the
   primary quantity.
4. **KSG uncertainty.** No interval method is calibrated. That is consistent
   with reporting KSG as sensitivity only.
5. **Subsampling intervals** under-cover. The √(m/n) rate is inappropriate
   for MI near zero.

**Until a revision amendment is committed, no real-data MI may be computed.**
