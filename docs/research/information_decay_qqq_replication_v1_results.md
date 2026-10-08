# M3 Information Decay v1 — QQQ Cross-Asset Replication: Result Record

**Status: ACCEPTED by independent review** (`QQQ REPLICATION RESULTS
ACCEPTED`). Frozen before any estimator-sensitivity or QQQ-residual
analysis: KSG, QQQ residual, CMI, alternative cohorts, other families and
decay fitting have not been run.

This record is **descriptive only** and introduces no methodology. QQQ is a
**replication / secondary** analysis, not a new primary hypothesis family, and
it is never pooled with SPY. The frozen SPY results were read, not recomputed.
Machine-readable results are in `data/reports/information_decay/qqq_replication_v1/`,
and provenance is in `metadata/research/information_decay_qqq_replication_v1.json`.

## 1. Identity and provenance

| Item | Value |
|---|---|
| Calibration freeze | tag `m3-information-decay-calibration-freeze` → `f08fe850c142fed595e847ce03d6931908587c6d` |
| SPY primary (accepted) | tag `m3-primary-spy-information-profile-v1` → `70ee4c1f54cb36eb7808fb5a647e5b7f2cd2aa71`. Outputs combined `31a05df0260b5f907fd79dcad611f41d6450feb02c4c3ca1e74db94dac649b60`, verified intact. |
| SPY residual (accepted) | tag `m3-secondary-spy-residual-profile-v1` → `eb2948bcafba8f5a2393efdcb47d81a8267ea5af`. Outputs combined `2216d6c7f3a2234fbb3d37d8643e4fa52ab1332e0d5b1bad5171960bbe802fc4`, verified intact. |
| Protocol commits | methodology `d976c3e408ec770ffa6633af22b422d3814e0e16`; Amendment 02 `e18d46e1110753923df6230ec388ca453b9ecd4e`; Amendment 03 `cef704ed34d971487ddcc5e9525da2ca1735e77a`; Amendment 04 `9f4bef157cd490f848cb3544ab92475def430edc` |
| QQQ runner | `scripts/run_information_decay_qqq_replication_v1.py`, commit **`026360afc8fb7fc26937b4fb5ad4dd7c086bc91a`** (code only; the run's HEAD). The frozen library is byte-identical to the calibration tag. A test proves the runner's symbol-parameterized panel analysis is bit-identical to the frozen primary analysis for SPY. |
| Implementation fingerprint | `1e19fee35ed1d592aaad7b5a14502d55251d2bfba9fd1fb70a9801960e924dc1` (29 files) |
| Run config | `config/information_decay_qqq_replication_v1.yaml`, sha256 `649824ffcaa0de85567ff2dec2e931454df3572ebacfd14b1e8a79cc097c1ac4` |
| Input dataset | `event_response_v2.parquet`: content sha256 `a6f83a05be0699032dba64e1d712d8d5d6f132dcdf154db8e890fd571c049986` (matches its recorded value) |
| Market dataset | `sha256:b4c6573e873774053183ecbe9a8a981431926795b8bcaaa9b6c440e7b654b1d7` (Alpaca SIP 1-minute) |
| Run timestamp | 2026-10-08T21:19:48Z |
| Working tree | No tracked file modified and no scientific file untracked. Untracked prose and the then-uncommitted metadata are recorded in `gates`. |

## 2. Scope and procedure

| Item | Value |
|---|---|
| Market | QQQ |
| Families | CPI m/m, Core CPI m/m, NFP |
| Response | raw `R_h` |
| Horizons | h ∈ {1, 5, 15, 30, 60} min |
| Cohort | QQQ's own `cs_raw`, the same frozen common-support rule as the SPY primary |
| Estimator | GCMI, tie policy B |

The estimator is **permutation-null-adjusted Gaussian-copula dependence in
bits**. It is primarily sensitive to monotonic association and is not
universal nonlinear MI.

Inference:

- 10,000 permutations plus the observed statistic (p floor 1/10,001), drawn
  within the chronological strata (2016–2020, 2021–2022, 2023–2026-09) and
  shared across the five horizons;
- a horizon max-statistic within each family;
- the **QQQ replication family correction**: Holm across the three QQQ
  panels, separate from the frozen SPY family and never pooled with it;
- a 2,000-replicate stratified release-level percentile bootstrap, with tie
  policy B inside every replicate.

QQQ has poorer historical pre-market coverage than SPY. The common-support
rule was **not** weakened, no SPY data were substituted, nothing was patched
from another provider, and nothing was interpolated. The reduced QQQ cohort
is part of the result.

## 3. QQQ cohorts

| Family | n | Release-ID sha256 | dev / val / test |
|---|---|---|---|
| CPI m/m | **80** | `abac905eec34ccb91aad65680ab2138b73b0ce8c614b8229a399001cb0ca1866` | 16 / 21 / 43 |
| Core CPI m/m | **80** | `abac905eec34ccb91aad65680ab2138b73b0ce8c614b8229a399001cb0ca1866` | 16 / 21 / 43 |
| NFP | **89** | `e12795fdd5b018569ac79058d74073b8176dba4c2262d745c2fbe6b435caadf3` | 27 / 21 / 41 |

- These counts equal the frozen inventory (methodology §7.2).
- Each family has one observation per release, and all five horizons use
  the same releases.
- CPI m/m and Core CPI m/m use the **same 80 releases and identical QQQ
  response vectors**. They are dependent panels, not independent
  replications. Holm remains valid under dependence.

Exclusions by first failing rule (QQQ):

| Family | Surprise history < 12 | Event excluded | Exchange closed | Missing pre-market minute (`insufficient_market_window`) |
|---|---|---|---|---|
| CPI / Core CPI | 12 | 0 | 2 | 33 (7 at 1m, 4 at 5m, 7 at 15m, 6 at 30m, 9 at 60m) |
| NFP | 12 | 1 | 3 | 24 (5 at 1m, 1 at 5m, 4 at 15m, 3 at 30m, 11 at 60m) |

For comparison, SPY's missing-minute exclusions are 20 for the CPI families
and 10 for NFP.

## 4. QQQ information profile

All values are in bits. Effective = GCMI − permutation-null mean, not
truncated. p = 0.0001 is the floor 1/10,001.

The interval is a **nominal 95% percentile bootstrap interval of effective
GCMI**. In synthetic calibration its observed coverage was about **0.93**,
not 0.95.

| Family | h (min) | n | GCMI raw | Null mean | **Effective** | p (pointwise) | p (horizon-adj.) | 95% bootstrap (effective) | Replication-Holm detectable |
|---|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 80 | 0.5559 | 0.0121 | **0.5438** | 0.0001 | 0.0001 | [0.3650, 0.7275] | yes |
| CPI m/m | 5 | 80 | 0.5136 | 0.0119 | **0.5018** | 0.0001 | 0.0001 | [0.3225, 0.6948] | yes |
| CPI m/m | 15 | 80 | 0.4076 | 0.0113 | **0.3963** | 0.0001 | 0.0001 | [0.2285, 0.6010] | yes |
| CPI m/m | 30 | 80 | 0.3738 | 0.0115 | **0.3623** | 0.0001 | 0.0001 | [0.1913, 0.5726] | yes |
| CPI m/m | 60* | 80 | 0.3278 | 0.0111 | **0.3167** | 0.0001 | 0.0001 | [0.1518, 0.5201] | yes |
| Core CPI m/m | 1 | 80 | 0.5851 | 0.0123 | **0.5727** | 0.0001 | 0.0001 | [0.3926, 0.7646] | yes |
| Core CPI m/m | 5 | 80 | 0.5487 | 0.0124 | **0.5363** | 0.0001 | 0.0001 | [0.3663, 0.7008] | yes |
| Core CPI m/m | 15 | 80 | 0.4101 | 0.0127 | **0.3974** | 0.0001 | 0.0001 | [0.2299, 0.5876] | yes |
| Core CPI m/m | 30 | 80 | 0.3546 | 0.0130 | **0.3416** | 0.0001 | 0.0001 | [0.1846, 0.5187] | yes |
| Core CPI m/m | 60* | 80 | 0.3529 | 0.0128 | **0.3401** | 0.0001 | 0.0001 | [0.1873, 0.5211] | yes |
| NFP | 1 | 89 | 0.0042 | 0.0061 | **-0.0020** | 0.4118 | 0.7548 | [-0.0061, 0.0350] | no |
| NFP | 5 | 89 | 0.0118 | 0.0067 | **0.0051** | 0.1855 | 0.3830 | [-0.0067, 0.0536] | no |
| NFP | 15 | 89 | 0.0081 | 0.0073 | **0.0009** | 0.2929 | 0.5229 | [-0.0073, 0.0564] | no |
| NFP | 30 | 89 | 0.0016 | 0.0067 | **-0.0051** | 0.6290 | 0.9345 | [-0.0067, 0.0278] | no |
| NFP | 60* | 89 | 0.0000 | 0.0072 | **-0.0072** | 0.9964 | 1.0000 | [-0.0072, 0.0207] | no |

\* 60 min is session-boundary-adjacent (§11).

## 5. QQQ replication family correction (Holm, FWER 0.05, three QQQ panels)

| Family | Panel p (min horizon-adj.) | Holm rank | Threshold | Holm-adjusted p | Panel | Detectable horizons |
|---|---|---|---|---|---|---|
| Core CPI m/m | 0.0000999900 | 1 | 0.016667 | 0.00029997 | rejected | 1, 5, 15, 30, 60 |
| CPI m/m | 0.0000999900 | 2 | 0.025 | 0.00029997 | rejected | 1, 5, 15, 30, 60 |
| NFP | 0.382962 | 3 | 0.05 | 0.382962 | not rejected | none |

QQQ and SPY p-values were **not** combined into a six-panel family. The SPY
primary family stays frozen.

## 6. Frozen SPY full-cohort comparison (descriptive; different cohorts)

The SPY values come from the frozen primary outputs and were not
recomputed. No SPY-vs-QQQ difference test was pre-specified, and neither
market is called statistically stronger.

| Family | h (min) | SPY n | SPY effective (frozen) | SPY p adj. | QQQ n | QQQ effective | QQQ p adj. | QQQ − SPY (descriptive) |
|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 93 | 0.4889 | 0.0001 | 80 | 0.5438 | 0.0001 | +0.0549 |
| CPI m/m | 5 | 93 | 0.4420 | 0.0001 | 80 | 0.5018 | 0.0001 | +0.0598 |
| CPI m/m | 15 | 93 | 0.3334 | 0.0001 | 80 | 0.3963 | 0.0001 | +0.0629 |
| CPI m/m | 30 | 93 | 0.3092 | 0.0001 | 80 | 0.3623 | 0.0001 | +0.0531 |
| CPI m/m | 60 | 93 | 0.2648 | 0.0001 | 80 | 0.3167 | 0.0001 | +0.0518 |
| Core CPI m/m | 1 | 93 | 0.4929 | 0.0001 | 80 | 0.5727 | 0.0001 | +0.0798 |
| Core CPI m/m | 5 | 93 | 0.4344 | 0.0001 | 80 | 0.5363 | 0.0001 | +0.1019 |
| Core CPI m/m | 15 | 93 | 0.2871 | 0.0001 | 80 | 0.3974 | 0.0001 | +0.1102 |
| Core CPI m/m | 30 | 93 | 0.2456 | 0.0001 | 80 | 0.3416 | 0.0001 | +0.0960 |
| Core CPI m/m | 60 | 93 | 0.2387 | 0.0001 | 80 | 0.3401 | 0.0001 | +0.1015 |
| NFP | 1 | 103 | 0.0150 | 0.1401 | 89 | -0.0020 | 0.7548 | -0.0170 |
| NFP | 5 | 103 | 0.0028 | 0.4338 | 89 | 0.0051 | 0.3830 | +0.0023 |
| NFP | 15 | 103 | 0.0106 | 0.1979 | 89 | 0.0009 | 0.5229 | -0.0097 |
| NFP | 30 | 103 | 0.0113 | 0.2000 | 89 | -0.0051 | 0.9345 | -0.0163 |
| NFP | 60 | 103 | 0.0146 | 0.1420 | 89 | -0.0072 | 1.0000 | -0.0218 |

## 7. Cohort overlap

| Family | SPY primary n | QQQ n | Intersection | SPY-only | QQQ-only |
|---|---|---|---|---|---|
| CPI m/m | 93 | 80 | 80 | 13 | 0 |
| Core CPI m/m | 93 | 80 | 80 | 13 | 0 |
| NFP | 103 | 89 | 85 | 18 | 4 |

- For CPI and Core CPI, QQQ's cohort is a strict subset of SPY's. The 13
  SPY-only releases fall between 2017-09 and 2021-08, mainly in the early
  years when QQQ's pre-market coverage is sparse.
- For NFP, 4 releases are QQQ-only (§9).

## 8. Matched SPY–QQQ comparison (descriptive matched-cohort sensitivity)

Both assets are estimated on **exactly the same release IDs**: the exact
SPY ∩ QQQ intersection, as decided before the run.

- They share the permutation draws and the bootstrap-index draws (the keys
  carry no symbol). The bootstrap tie-break keys are symbol-specific.
- For CPI and Core CPI the intersection is the full QQQ cohort (80), so the
  QQQ-on-intersection values equal the QQQ replication values.
- No family-level significance decision is made for this sensitivity.

| Family | h (min) | n (both) | SPY-on-intersection effective | 95% bootstrap | QQQ-on-intersection effective | 95% bootstrap | Matched QQQ − SPY (descriptive) |
|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 80 | 0.5517 | [0.3720, 0.7293] | 0.5438 | [0.3650, 0.7275] | -0.0079 |
| CPI m/m | 5 | 80 | 0.4841 | [0.3134, 0.6691] | 0.5018 | [0.3225, 0.6948] | +0.0177 |
| CPI m/m | 15 | 80 | 0.3502 | [0.1938, 0.5464] | 0.3963 | [0.2285, 0.6010] | +0.0461 |
| CPI m/m | 30 | 80 | 0.3415 | [0.1821, 0.5401] | 0.3623 | [0.1913, 0.5726] | +0.0208 |
| CPI m/m | 60 | 80 | 0.2990 | [0.1385, 0.5047] | 0.3167 | [0.1518, 0.5201] | +0.0177 |
| Core CPI m/m | 1 | 80 | 0.5912 | [0.4162, 0.7787] | 0.5727 | [0.3926, 0.7646] | -0.0184205794 |
| Core CPI m/m | 5 | 80 | 0.5102 | [0.3418, 0.6852] | 0.5363 | [0.3663, 0.7008] | +0.0261181874 |
| Core CPI m/m | 15 | 80 | 0.3130 | [0.1654, 0.4957] | 0.3974 | [0.2299, 0.5876] | +0.0843557692 |
| Core CPI m/m | 30 | 80 | 0.2905 | [0.1463, 0.4620] | 0.3416 | [0.1846, 0.5187] | +0.0511073372 |
| Core CPI m/m | 60 | 80 | 0.2947 | [0.1403, 0.4923] | 0.3401 | [0.1873, 0.5211] | +0.0454221544 |
| NFP | 1 | 85 | -0.0060 | [-0.0079, 0.0340] | -0.0025 | [-0.0065, 0.0380] | +0.0035 |
| NFP | 5 | 85 | -0.0092 | [-0.0092, 0.0316] | 0.0039 | [-0.0067, 0.0545] | +0.0130 |
| NFP | 15 | 85 | -0.0081 | [-0.0096, 0.0366] | 0.0009 | [-0.0073, 0.0607] | +0.0089 |
| NFP | 30 | 85 | -0.0034 | [-0.0088, 0.0463] | -0.0054 | [-0.0065, 0.0291] | -0.0021 |
| NFP | 60 | 85 | 0.0003 | [-0.0085, 0.0546] | -0.0071 | [-0.0071, 0.0191] | -0.0074 |

**Matched QQQ − SPY difference for Core CPI m/m.**

| h | Difference |
|---|---|
| 15m | **+0.0843557692** bits |
| 30m | **+0.0511073372** bits |
| 60m | **+0.0454221544** bits |

- These are descriptive point differences.
- **No cross-asset difference test was pre-specified.**
- The matched bootstrap intervals of the two assets **overlap** at every
  horizon.
- They are **not evidence of structural heterogeneity**.

## 9. NFP intersection handling

| Analysis | NFP n |
|---|---|
| **QQQ replication** | **89** (QQQ's full common-support cohort) |
| **Matched cross-asset sensitivity** | **QQQ = 85, SPY = 85** (exact intersection) |

Four QQQ-only NFP releases are excluded from the matched comparison. For each
of them SPY has no complete response window (`insufficient_market_window`),
and no SPY data were patched or substituted:

- 2018-09-07
- 2020-11-06
- 2021-09-03
- 2025-02-07

The QQQ full-versus-matched differences are tiny (at most 0.0013 bits) and
descriptive only:

| h (min) | QQQ effective, full cohort (n = 89) | QQQ effective, matched (n = 85) | Full − matched |
|---|---|---|---|
| 1 | -0.001989 | -0.002539 | +0.000550 |
| 5 | 0.005091 | 0.003880 | +0.001211 |
| 15 | 0.000852 | 0.000852 | +0.000000 |
| 30 | -0.005051 | -0.005447 | +0.000395 |
| 60 | -0.007180 | -0.007092 | -0.000088 |

## 10. Interpretation (statistical dependence; no causal claim)

**CPI-family replication.** The CPI-family surprise-response association
observed in SPY is also detected in QQQ under the same frozen procedure. This
is **cross-asset consistency / replication, not independent experimental
replication**, because SPY and QQQ respond to the same macro releases.

- **CPI m/m (QQQ, n = 80).** Detectable at every horizon (replication Holm).
  Effective GCMI is highest at 1 minute (0.5438 bits), and the point
  estimates are lower at longer horizons (0.3167 bits at 60 minutes). The
  association is negative, as in SPY.
- **Core CPI m/m (QQQ, n = 80).** Detectable at every horizon (replication
  Holm). Effective GCMI is highest at 1 minute (0.5727 bits), with lower
  point estimates at longer horizons (0.3401 bits at 60 minutes). It shares
  its releases and responses with CPI m/m and is not an independent panel.

**Cohort composition.** Much of the apparent full-cohort difference is
associated with cohort composition; matched-cohort profiles are broadly
similar.

| Comparison | CPI m/m | Core CPI m/m |
|---|---|---|
| QQQ − SPY, full cohorts (§6) | +0.052 to +0.063 bits | +0.080 to +0.110 bits |
| Matched QQQ − SPY, same releases (§8) | −0.008 to +0.046 bits | −0.018 to +0.084 bits |

On the same releases, SPY's own effective GCMI is higher than on its full 93
releases: CPI 1m is 0.5517 vs 0.4889. The remaining matched cross-asset
difference (Core CPI 15–60m) is descriptive only (§8).

**NFP.** No detectable GCMI association was found in either SPY primary or
QQQ replication under their respective frozen common-support cohorts. No
claim is made about whether NFP has a market effect.

**Information profile, not decay.** Statistical information decay is not
established by this replication analysis. The CPI-family point estimates
being highest at 1 minute and lower later is descriptive only. The bootstrap
intervals overlap across horizons, and no half-life, exponential model,
decay constant or shape test was fitted or pre-specified.

## 11. Correlation benchmarks (not substitutes for GCMI)

| Family | h (min) | QQQ Pearson r | p (two-sided) | QQQ Spearman ρ | p (two-sided) | I_lin (bits) | QQQ effective GCMI |
|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | -0.586 | 0.0002 | -0.721 | 0.0001 | 0.3030 | 0.5438 |
| CPI m/m | 5 | -0.562 | 0.0002 | -0.727 | 0.0001 | 0.2740 | 0.5018 |
| CPI m/m | 15 | -0.517 | 0.0004 | -0.660 | 0.0001 | 0.2247 | 0.3963 |
| CPI m/m | 30 | -0.540 | 0.0003 | -0.649 | 0.0001 | 0.2482 | 0.3623 |
| CPI m/m | 60 | -0.523 | 0.0005 | -0.586 | 0.0001 | 0.2305 | 0.3167 |
| Core CPI m/m | 1 | -0.531 | 0.0001 | -0.749 | 0.0001 | 0.2393 | 0.5727 |
| Core CPI m/m | 5 | -0.518 | 0.0002 | -0.723 | 0.0001 | 0.2249 | 0.5363 |
| Core CPI m/m | 15 | -0.477 | 0.0006 | -0.646 | 0.0001 | 0.1860 | 0.3974 |
| Core CPI m/m | 30 | -0.499 | 0.0002 | -0.596 | 0.0001 | 0.2065 | 0.3416 |
| Core CPI m/m | 60 | -0.486 | 0.0004 | -0.590 | 0.0001 | 0.1948 | 0.3401 |
| NFP | 1 | +0.090 | 0.1251 | -0.111 | 0.2564 | 0.0058 | -0.0020 |
| NFP | 5 | +0.027 | 0.7046 | -0.144 | 0.1554 | 0.0005 | 0.0051 |
| NFP | 15 | +0.010 | 0.9151 | -0.137 | 0.1856 | 0.0001 | 0.0009 |
| NFP | 30 | +0.014 | 0.8363 | -0.071 | 0.4909 | 0.0001 | -0.0051 |
| NFP | 60 | +0.049 | 0.5458 | -0.021 | 0.8480 | 0.0017 | -0.0072 |

- **QQQ CPI family:** Spearman ρ (−0.59 to −0.75) is larger in magnitude
  than Pearson r (−0.48 to −0.59), and effective GCMI exceeds `I_lin`. This
  is the same pattern as in SPY.
- **QQQ NFP:** Pearson r is small and positive, Spearman ρ small and
  negative, and neither is significant. These divergences were not
  investigated and are not interpreted.

## 12. 60-minute session-boundary caveat

All releases are at 08:30 ET. The +60-minute response ends at the
[09:29, 09:30) bar, at the 09:30 regular-session open, so the 60-minute point
is **session-boundary-adjacent**. A change between 30 and 60 minutes is not
interpreted as pure information absorption.

## 13. Limitations

- **Not independent.** SPY and QQQ respond to the same releases, so QQQ does
  not double the sample. This is cross-asset consistency, not independent
  replication.
- **Smaller cohorts.** QQQ has fewer releases (80 / 89) because of sparser
  early pre-market coverage. This shifts the composition toward later years
  (development stratum 16 vs 27 for the CPI families).
- **No difference test.** No SPY-vs-QQQ difference test was pre-specified.
  All cross-asset differences are descriptive.
- **Dependent panels.** CPI and Core CPI are dependent: identical QQQ
  releases and responses.
- **Estimator scope.** GCMI captures primarily monotonic dependence; KSG has
  not been run.
- **Interval coverage.** Intervals are nominal 95% with about 0.93 synthetic
  coverage.
- **p-value resolution.** The smallest attainable p is 1/10,001.
- **Raw response only.** No QQQ residual analysis has been run.
- **Retrospective historical inference.** Forecasts are not independently
  verified as point-in-time.

## 14. Output hashes

Outputs are in `data/reports/information_decay/qqq_replication_v1/`,
combined sha256 **`3d5e8f593f157fca8fe742e0779b781145a4e9ea2e28a6ee8bc34f8616ae8932`**.

| File | sha256 |
|---|---|
| `cohort_overlap.csv` | `fa70fccc00e0275a03a33ee76a1925e5df17f3cc51de8d168069d2516a562986` |
| `cohort_release_ids.csv` | `44a4a3d3a2062117e369ca2d82c6ef72a509be0cf1e379d03051b5548fe26abf` |
| `plot1_qqq_information_profile.svg` | `f81c6836e0b82e947ba6920cf74270f8e3dbf55ed26ba25d9910dce295318ae1` |
| `plot2_spy_vs_qqq.svg` | `2cf80762121fa3e28052e198da0cbe60fed1839bf7ee2897395d342a440dd88f` |
| `plot3_spy_matched_vs_qqq.svg` | `a7b86461d46ca76d34d1f466160fe667b1176a37779ca48481c9825c6360f2ce` |
| `plot4_qqq_correlation_benchmarks.svg` | `6cc6569ccf6c380cafb358b539d095ff77339812b43201995ba7ee607b4d8672` |
| `qqq_family_holm.csv` | `9fe2fc6395a951506817eea94fc4347893d6b39e0088ae338bbd64123f61273c` |
| `qqq_profile.csv` | `e355949d6eb5657325e2a308537f127aa7a03b37a232bca05d141cc065354ded` |
| `spy_matched_qqq_cohort.csv` | `dde3fe38543441fd752ad890b01985b216057f91a1be8d3486bc0d109e8153fa` |
| `spy_matched_qqq_comparison.csv` | `35bdf698f0c20ca3ee70454f0f588bfbb0b7a41dda9e089a66d80a9a9855e45f` |
| `spy_qqq_comparison.csv` | `2614ffc0e78daedc4ea39f9773464eff7f47978736457b48f9329494381a6338` |
| `summary.json` | `c6db458bf8f8246be42212b6703255eb099cd8bb1b970d96fc16b4b91bf88c8c` |

An independent rerun reproduced every output byte for byte.

## 15. Independent review

The independent review verdict is **`QQQ REPLICATION RESULTS ACCEPTED`**.
These results are frozen as tag `m3-qqq-replication-v1`, before KSG
estimator-sensitivity or QQQ-residual analyses.
