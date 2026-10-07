# M3 Information Decay v1 — Amendment 02 Revalidation Results

| Field | Value |
|---|---|
| Protocol | `docs/research/information_decay_v1_amendment_02.md`, committed **before** these results at `e18d46e1110753923df6230ec388ca453b9ecd4e` (blob `d852f4a6…`, verified unchanged at run time) |
| REAL MARKET MI COMPUTED | **NO** (synthetic only; ran under `isolation.real_data_guard`) |
| Status | Results for independent review — **uncommitted** |
| Run | 2026-10-07T20:13Z start, 8 workers |
| Configs | base `config/information_decay_v1.yaml` sha256 `bd877f7d…5c3` (unchanged since Amendment 01); `config/information_decay_v1_amendment02.yaml` sha256 `9b5cfc62…bff4` |
| Implementation | uncommitted-tree sha256 `b73d9457…4b62` |
| Outputs | `data/reports/information_decay/amendment02_revalidation/` (combined sha256 `782efec1…11ee`); provenance in `metadata/research/information_decay_amendment02_revalidation.json` |
| Amendment-01 inputs | hash-verified before use |

## Verdict

**GCMI DOES NOT QUALIFY** under the frozen Amendment-02 rule.

- **No hard cell FAILED.** 38 of 42 are PASS.
- **Four cells stayed INCONCLUSIVE** after the full 10,000 replicates. Their
  rates are 0.053–0.054, all *below* the 0.0597 ceiling, but not far enough
  below it for the simultaneous Holm–Wilson rule (α = 0.01 per look) to
  decide.
- The rule's pre-run operating characteristic predicted this. A test whose
  true FPR is exactly nominal qualifies with probability ≈ 0.22 (Amendment 02
  §4).
- As Amendment 02 states, this STOP is **not, by itself, evidence of
  miscalibration**. It is the outcome the strict rule produces, and
  real-data MI remains blocked.

## Hard-gate family 𝓗 (machine-readable: `hard_gate_cells.csv`, `look_history.csv`)

| Gate | PASS | INCONCLUSIVE | FAIL |
|---|---|---|---|
| V1 (24 cells) | 22 | 2 | 0 |
| D3 policy A (6 cells) | 5 | 1 | 0 |
| D3 policy B (6 cells) | **6** | 0 | 0 |
| V4a (6 cells) | 5 | 1 | 0 |

All four INCONCLUSIVE cells, listed programmatically in `flagged_cells.csv`:

| Gate | Cell | n | R | Rate | Ceiling |
|---|---|---|---|---|---|
| V1 | `s1_null_modeB` | 80 | 10,000 | 0.0533 | 0.0597 |
| V1 | `s6_null_t3` | 80 | 10,000 | 0.0531 | 0.0597 |
| D3/V2 | `s9_null_discrete`, policy A | 100 | 10,000 | 0.0531 | 0.0597 |
| V4a | `s7v4a_null_independent_contamination` | 77 | 10,000 | 0.0543 | 0.0597 |

Every PASS cell's rate lies between 0.039 and 0.0525.

## D3 — tie policy

- Policy **A** is INCONCLUSIVE (n = 100).
- Policy **B** is PASS at all six sizes.
- Rule outcome: **B selected**, because only B passes. A's single cell is
  undecided at a rate of 0.0531, and no A cell failed.

## Gate status for qualification

| Gate | Status |
|---|---|
| V1 | **INCONCLUSIVE** (2 cells) |
| V2 (policy B) | PASS |
| V3 | PASS (30 of 30 cells; from stored rows, cross-checked by reproduction) |
| V4a | **INCONCLUSIVE** (1 cell) |
| Tests | pass |

## Secondary residual family 𝓡 — Null B under policy B (`residual_family_cells.csv`)

**All 12 cells PASS** (rates 0.041–0.053). The cells are `s8_null_t3` and
`s8_null_modeB_disc` at n ∈ {60, 77, 80, 97, 100, 120}.

Null A diagnostic, policy B, first 2,000 replicates: still inflated under
heavy tails, at **0.096–0.125**, and 0.047–0.059 in the discrete cell. This
confirms that Null A is invalid for residual inference.

## Coverage (D5), cluster-aware (`coverage_cluster.csv`)

The stratified percentile bootstrap is **selected** by the frozen rule. Its
minimum coverage over the ρ ≥ 0.25 cells is 0.928, which meets the 0.90
eligibility bar. The recentred bootstrap scores 0.910, and subsampling is
ineligible at 0.584.

Replicate-cluster point coverage, with 95% MC intervals:

| Cell | Coverage range over n | Cluster SE |
|---|---|---|
| ρ = 0.15 | 0.983–0.988 (over-covers) | 0.0012–0.0016 |
| ρ = 0.25 | 0.934–0.953 | 0.0024–0.0031 |
| ρ = 0.35, n = 60…120 incl. 77/97 | **0.929–0.935** | ≈ 0.003 |
| ρ = 0.50 | 0.931–0.935 | ≈ 0.003 |
| Copula s3a | 0.928–0.936 | ≈ 0.003 |

Per-horizon coverage at ρ = 0.35 ranges from 0.921 to 0.942.

Wording, if used: a *nominal 95% percentile bootstrap interval with
empirically observed synthetic coverage of approximately 0.93 under the
tested scenarios*. It is **not** a calibrated 95% interval.

## Reproduction of Amendment 01

- **394 / 394 checks reproduced** within the stored 10-significant-digit
  precision. The checks compare first-batch rates, means, null means,
  effective MI and pooled coverage.
- The corrected code therefore changes no previously computed GCMI quantity.
- The residual heavy-tail cell under policy B equals the stored policy-A
  values, because its surprise is continuous and the policies coincide.

## V4a / V4b

**V4a, KSG descriptive** (first 1,000 replicates, not classified): rejection
rates 0.044–0.062.

**V4b sensitivity, gcmi / ksg5 / ksg10.** In `s7_alt_contaminated` the
contaminated truth is 0.23–0.29 bits. Every estimator sits **far below** it:

| Estimator | `MI_eff` (bits) | Influence (bits) |
|---|---|---|
| GCMI | 0.025–0.034 | 0.053–0.064 |
| KSG k=5 | 0.107–0.114 | 0.027–0.033 |
| KSG k=10 | 0.095–0.108 | 0.021–0.025 |

- KSG's distance from the *clean* target (+0.001 to +0.019 bits) is not
  overestimation.
- GCMI's power falls from 0.90–0.99 (clean) to 0.48–0.81 (contaminated).
  Rank transformation places adversarial outliers at extreme scores.
- In `s7_null_contaminated`, the contaminants encode dependence, so the
  rejection rates of 0.09–0.20 are detections, not false positives.

## Process notes

- **First run stopped.** The first revalidation run was stopped during the
  residual stage. Its policy-B Null-B scoring looped per permutation and
  projected to 2–3 h. Nothing was written. The scorer was replaced by a
  vectorized implementation that is bit-identical to the loop (exact-equality
  test, including tied medians), and the run restarted from scratch. Its
  hard-family batch sizes matched the stopped run look for look.
- **Runtime.** The hard family took 7 min, the residual family 49 min and
  coverage 3.5 min.

## Implications for review (not decisions)

The rule chosen in Amendment 02 cannot certify a test whose true FPR is at
nominal with useful probability. Under it, GCMI's evidence is:

- 38/42 PASS and 0 FAIL;
- every undecided cell below the ceiling;
- exact reproduction.

The verdict is nonetheless STOP, which is the rule working as designed.

Lifting the block requires **a further methodology amendment**, with its own
justification. For example, it could:

- define the ceiling on the *true* FPR scale (e.g. nominal + a tolerance)
  rather than reusing the Monte Carlo-error ceiling;
- choose a different simultaneous rule;
- raise the replicate cap.

Such a choice is the project owner's. Because these results are now known, it
should be justified on operating-characteristic grounds rather than on these
results.
