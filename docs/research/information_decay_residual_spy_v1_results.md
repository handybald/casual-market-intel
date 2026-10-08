# M3 Information Decay v1 — Secondary SPY Residual Information Profile: Result Record

**Status: ACCEPTED by independent review** (`SECONDARY SPY RESIDUAL RESULTS
ACCEPTED`). Frozen before any replication or sensitivity analysis: QQQ, KSG,
CMI, alternative baselines, decay fitting and other families have not been run.

This record is **descriptive only** and introduces no methodology. This
analysis is **secondary**. It does not replace or alter the frozen primary
result (`m3-primary-spy-information-profile-v1`). Machine-readable results
are in `data/reports/information_decay/residual_spy_v1/`, and provenance is
in `metadata/research/information_decay_residual_spy_v1.json`.

## 1. Identity and provenance

| Item | Value |
|---|---|
| Calibration freeze | tag `m3-information-decay-calibration-freeze` → `f08fe850c142fed595e847ce03d6931908587c6d` |
| Primary result | tag `m3-primary-spy-information-profile-v1` → `70ee4c1f54cb36eb7808fb5a647e5b7f2cd2aa71`. Its metadata was unchanged and its output files were hash-verified at run time. |
| Protocol commits | methodology `d976c3e408ec770ffa6633af22b422d3814e0e16`; Amendment 02 `e18d46e1110753923df6230ec388ca453b9ecd4e`; Amendment 03 `cef704ed34d971487ddcc5e9525da2ca1735e77a`; Amendment 04 `9f4bef157cd490f848cb3544ab92475def430edc` |
| Residual runner | `scripts/run_information_decay_residual_spy_v1.py`, commit **`097b92afac72a92df9a815d16375483fd18f8c6c`** (code only; the run's HEAD). The frozen library is byte-identical to the calibration tag. |
| Implementation fingerprint | `2512635e7b33f47200df766f7e5c26284aca44f0f7f4cdc3cbfb328dd6b35b65` (27 files) |
| Run config | `config/information_decay_residual_spy_v1.yaml`, sha256 `05880b008074ea1c926bf460797ac5a4daa1275cd9c7c3c30ab9cc9c8cd5a0a6` |
| Input dataset | `event_response_v2.parquet`: content sha256 `a6f83a05be0699032dba64e1d712d8d5d6f132dcdf154db8e890fd571c049986` (matches its recorded value), file sha256 `5bbdb16dc7256763d80c7bef39ae82be83ce7619f4cdee0091410e80364f3e03` |
| Market dataset | `sha256:b4c6573e873774053183ecbe9a8a981431926795b8bcaaa9b6c440e7b654b1d7` |
| Run timestamp | 2026-10-08T20:16:00Z |
| Working tree | No tracked file modified and no scientific file untracked. Untracked prose and the then-uncommitted metadata are recorded in `gates`. |

## 2. Scope and estimator

| Item | Value |
|---|---|
| Market | SPY |
| Families | CPI m/m, Core CPI m/m, NFP |
| Horizons | h ∈ {1, 5, 15, 30, 60} min |
| Cohort | residual common support `cs_resid` |
| Estimator | GCMI, tie policy B |

The estimator is **permutation-null-adjusted Gaussian-copula dependence in
bits**. It is primarily sensitive to monotonic association and is not a
universal nonlinear MI estimator.

- **Residual:** `I(S; E_h)` with `E_h = R_h − Rhat_h`, where `Rhat_h` is the
  frozen M2 walk-forward expected response.
- **Matched raw:** `I(S; R_h)` on **exactly the same** `cs_resid` releases.
  This is the valid like-for-like comparator. The primary n = 93/93/103
  profile is context only and is **not** used to quantify residualization.
- **Inference:** 10,000 permutations plus the observed statistic (p floor
  1/10,001), drawn within the chronological strata (2016–2020, 2021–2022,
  2023–2026-09) and shared across the five horizons.
  - Residual: **pipeline-aware Null B** (§7).
  - Matched raw: stratified release-level permutation with the response fixed.
- **Multiplicity:** a horizon max-statistic within each family and response
  type, plus a **secondary residual family-level correction** (Holm, 3
  panels).
- **Uncertainty:** a stratified release-level percentile bootstrap with 2,000
  replicates. Tie policy B is applied inside every replicate, and the bootstrap
  resamples are shared between matched raw and residual.

## 3. Residual cohorts

| Family | Rows | Label pool (valid S) | Primary cs_raw (context) | Matched raw = residual n | Release-ID sha256 | dev / val / test |
|---|---|---|---|---|---|---|
| CPI m/m | 127 | 115 | 93 | **77** | `bcb453012e2f4fe27540d347bf176e88e5c88c760a2e37731c767bf91d2538bb` | 11 / 23 / 43 |
| Core CPI m/m | 127 | 115 | 93 | **77** | `76544fdf32afbdcb2e3508ef7c771e88845f7365d96d803658e01b90acf66f8c` | 11 / 23 / 43 |
| NFP | 129 | 116 | 103 | **97** | `4bba280aa8c525e9aa29918dfc11218399a70c188f0a574eb9270a1124e25e81` | 35 / 22 / 40 |

- These counts equal the frozen pre-analysis inventory (methodology §7.2).
- All five horizons use identical releases within a family, and each family
  has one observation per release.
- **The CPI and Core CPI residual cohorts differ:** they share 75 releases,
  and each has 2 of its own. Each family's baseline groups releases by the
  sign of its own raw surprise, so the qualifying histories differ.
- The two families still share SPY responses on their common releases. They
  are **not independent replications**.

## 4. Matched raw (same releases as the residual)

Effective = GCMI − null mean (bits, not truncated). p = 0.0001 is the floor
1/10,001.

| Family | h (min) | n | Matched raw: GCMI | null mean | **effective** | p (pointwise) | p (horizon-adj.) | 95% bootstrap (effective) |
|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 77 | 0.5382 | 0.0116 | **0.5265** | 0.0001 | 0.0001 | [0.3442, 0.7184] |
| CPI m/m | 5 | 77 | 0.4682 | 0.0113 | **0.4569** | 0.0001 | 0.0001 | [0.2711, 0.6513] |
| CPI m/m | 15 | 77 | 0.3631 | 0.0108 | **0.3522** | 0.0001 | 0.0001 | [0.1818, 0.5494] |
| CPI m/m | 30 | 77 | 0.3280 | 0.0111 | **0.3169** | 0.0001 | 0.0001 | [0.1517, 0.5260] |
| CPI m/m | 60* | 77 | 0.3051 | 0.0108 | **0.2942** | 0.0001 | 0.0001 | [0.1315, 0.5079] |
| Core CPI m/m | 1 | 77 | 0.5233 | 0.0112 | **0.5121** | 0.0001 | 0.0001 | [0.3246, 0.7294] |
| Core CPI m/m | 5 | 77 | 0.4645 | 0.0112 | **0.4532** | 0.0001 | 0.0001 | [0.2708, 0.6471] |
| Core CPI m/m | 15 | 77 | 0.3129 | 0.0114 | **0.3015** | 0.0001 | 0.0001 | [0.1513, 0.4997] |
| Core CPI m/m | 30 | 77 | 0.2673 | 0.0114 | **0.2559** | 0.0001 | 0.0001 | [0.1118, 0.4310] |
| Core CPI m/m | 60* | 77 | 0.2755 | 0.0117 | **0.2638** | 0.0001 | 0.0001 | [0.1118, 0.4563] |
| NFP | 1 | 97 | 0.0118 | 0.0060 | **0.0058** | 0.1621 | 0.3589 | [-0.0060, 0.0521] |
| NFP | 5 | 97 | 0.0039 | 0.0065 | **-0.0026** | 0.4367 | 0.7484 | [-0.0065, 0.0406] |
| NFP | 15 | 97 | 0.0084 | 0.0067 | **0.0017** | 0.2591 | 0.4903 | [-0.0067, 0.0554] |
| NFP | 30 | 97 | 0.0087 | 0.0060 | **0.0027** | 0.2317 | 0.4797 | [-0.0059, 0.0539] |
| NFP | 60* | 97 | 0.0122 | 0.0060 | **0.0062** | 0.1563 | 0.3470 | [-0.0060, 0.0596] |

## 5. Residual

The null distribution, the effective subtraction and the max-T come from
**Null B**. The rightmost column is the secondary residual family-level
correction (§6).

| Family | h (min) | n | Residual: GCMI | Null-B mean | **effective** | p (pointwise, Null B) | p (horizon-adj., Null B max-T) | 95% bootstrap (effective) | Secondary-Holm detectable |
|---|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 77 | 0.1926 | 0.0060 | **0.1866** | 0.0001 | 0.0001 | [0.0595, 0.3583] | yes |
| CPI m/m | 5 | 77 | 0.0905 | 0.0059 | **0.0846** | 0.0002 | 0.0002 | [0.0065, 0.2085] | yes |
| CPI m/m | 15 | 77 | 0.0438 | 0.0056 | **0.0383** | 0.0049 | 0.0123 | [-0.0048, 0.1431] | yes |
| CPI m/m | 30 | 77 | 0.0318 | 0.0049 | **0.0269** | 0.0105 | 0.0381 | [-0.0047, 0.1237] | no |
| CPI m/m | 60* | 77 | 0.0275 | 0.0048 | **0.0227** | 0.0161 | 0.0587 | [-0.0047, 0.1162] | no |
| Core CPI m/m | 1 | 77 | 0.1490 | 0.0065 | **0.1426** | 0.0001 | 0.0001 | [0.0380, 0.3002] | yes |
| Core CPI m/m | 5 | 77 | 0.0740 | 0.0063 | **0.0677** | 0.0004 | 0.0008 | [0.0019, 0.1877] | yes |
| Core CPI m/m | 15 | 77 | 0.0394 | 0.0060 | **0.0334** | 0.0097 | 0.0260 | [-0.0054, 0.1359] | no |
| Core CPI m/m | 30 | 77 | 0.0350 | 0.0056 | **0.0294** | 0.0125 | 0.0376 | [-0.0053, 0.1278] | no |
| Core CPI m/m | 60* | 77 | 0.0181 | 0.0052 | **0.0128** | 0.0648 | 0.1695 | [-0.0052, 0.0979] | no |
| NFP | 1 | 97 | 0.0009 | 0.0082 | **-0.0074** | 0.8184 | 0.9746 | [-0.0082, 0.0272] | no |
| NFP | 5 | 97 | 0.0054 | 0.0083 | **-0.0029** | 0.5023 | 0.6674 | [-0.0083, 0.0527] | no |
| NFP | 15 | 97 | 0.0031 | 0.0059 | **-0.0029** | 0.5111 | 0.8234 | [-0.0059, 0.0465] | no |
| NFP | 30 | 97 | 0.0006 | 0.0039 | **-0.0033** | 0.6936 | 0.9883 | [-0.0039, 0.0288] | no |
| NFP | 60* | 97 | 0.0006 | 0.0037 | **-0.0032** | 0.6975 | 0.9898 | [-0.0037, 0.0323] | no |

\* 60 min is session-boundary-adjacent (§9).

The NFP residual effective values are slightly negative: the Null-B mean
exceeds the observed value. They are reported as they are.

## 6. Secondary residual family-level correction (Holm, FWER 0.05, 3 panels)

| Family | Panel p (min horizon-adj.) | Holm rank | Threshold | Holm-adjusted p | Panel | Detectable horizons |
|---|---|---|---|---|---|---|
| Core CPI m/m | 0.0000999900 | 1 | 0.016667 | 0.00029997 | rejected | 1, 5 |
| CPI m/m | 0.0000999900 | 2 | 0.025 | 0.00029997 | rejected | 1, 5, 15 |
| NFP | 0.667433 | 3 | 0.05 | 0.667433 | not rejected | none |

This correction family is separate from the primary Holm family. They were
not pooled. Matched-raw significance is descriptive or supporting only and
carries no family-level decision.

## 7. Descriptive residual / matched-raw comparison

Δ and the ratio are computed from full-precision effective GCMI on identical
releases.

| Family | h (min) | Matched-raw effective | Residual effective | Δ (residual − matched raw) | Residual / matched raw |
|---|---|---|---|---|---|
| CPI m/m | 1 | 0.5265 | 0.1866 | -0.3399 | 35.4374% |
| CPI m/m | 5 | 0.4569 | 0.0846 | -0.3722 | 18.5258% |
| CPI m/m | 15 | 0.3522 | 0.0383 | -0.3140 | 10.8629% |
| CPI m/m | 30 | 0.3169 | 0.0269 | -0.2900 | 8.4875% |
| CPI m/m | 60* | 0.2942 | 0.0227 | -0.2716 | 7.6990% |
| Core CPI m/m | 1 | 0.5121 | 0.1426 | -0.3695 | 27.8406% |
| Core CPI m/m | 5 | 0.4532 | 0.0677 | -0.3855 | 14.9428% |
| Core CPI m/m | 15 | 0.3015 | 0.0334 | -0.2681 | 11.0623% |
| Core CPI m/m | 30 | 0.2559 | 0.0294 | -0.2265 | 11.4867% |
| Core CPI m/m | 60* | 0.2638 | 0.0128 | -0.2509 | 4.8669% |
| NFP | 1 | 0.0058 | -0.0074 | -0.0132 | not meaningful (near-zero / negative values) |
| NFP | 5 | -0.0026 | -0.0029 | -0.0003 | not meaningful (near-zero / negative values) |
| NFP | 15 | 0.0017 | -0.0029 | -0.0046 | not meaningful (near-zero / negative values) |
| NFP | 30 | 0.0027 | -0.0033 | -0.0060 | not meaningful (near-zero / negative values) |
| NFP | 60* | 0.0062 | -0.0032 | -0.0093 | not meaningful (near-zero / negative values) |

**The raw-to-residual difference is descriptive and is not an additive
explained-information decomposition.** No delta-specific test was frozen, so
neither Δ nor the ratio carries a significance claim. Mutual information is
not additive across a baseline subtraction, and the ratio is not a share of
information "explained".

The CPI-family reduction is consistent with the methodology's pre-registered
analytic expectation (§2.1(3)): subtracting the historical sign-group mean
removes much of a monotonic signal by construction.

## 8. Correlation benchmarks (not substitutes for GCMI)

| Family | h (min) | Residual Pearson r | p (Null B) | Residual Spearman ρ | p (Null B) | Matched-raw Pearson r | Matched-raw Spearman ρ |
|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | -0.460 | 0.0003 | -0.408 | 0.0001 | -0.589 | -0.708 |
| CPI m/m | 5 | -0.381 | 0.0038 | -0.272 | 0.0021 | -0.535 | -0.700 |
| CPI m/m | 15 | -0.301 | 0.0223 | -0.143 | 0.1012 | -0.479 | -0.619 |
| CPI m/m | 30 | -0.282 | 0.0270 | -0.122 | 0.1337 | -0.498 | -0.598 |
| CPI m/m | 60 | -0.300 | 0.0165 | -0.070 | 0.4248 | -0.491 | -0.557 |
| Core CPI m/m | 1 | -0.419 | 0.0011 | -0.358 | 0.0001 | -0.562 | -0.703 |
| Core CPI m/m | 5 | -0.331 | 0.0138 | -0.208 | 0.0199 | -0.507 | -0.660 |
| Core CPI m/m | 15 | -0.275 | 0.0373 | -0.133 | 0.1314 | -0.453 | -0.565 |
| Core CPI m/m | 30 | -0.259 | 0.0418 | -0.121 | 0.1422 | -0.466 | -0.507 |
| Core CPI m/m | 60 | -0.225 | 0.0879 | -0.054 | 0.5336 | -0.449 | -0.513 |
| NFP | 1 | +0.084 | 0.1978 | -0.087 | 0.5547 | +0.129 | +0.100 |
| NFP | 5 | +0.068 | 0.3771 | -0.125 | 0.3263 | +0.119 | +0.059 |
| NFP | 15 | +0.151 | 0.0287 | -0.116 | 0.2593 | +0.198 | +0.073 |
| NFP | 30 | +0.144 | 0.0457 | -0.067 | 0.4109 | +0.182 | +0.087 |
| NFP | 60 | +0.210 | 0.0191 | -0.076 | 0.3260 | +0.252 | +0.108 |

## 9. Interpretation (statistical dependence; no causal claim)

`I(S; E_h)` is **baseline-adjusted dependence**.

**CPI m/m.**

- A substantial portion of the raw CPI-family surprise-response association
  is not present after subtracting the frozen sign-conditioned walk-forward
  expected response.
- On identical releases, residual effective GCMI is **35.4374%** of matched
  raw at 1 minute and 7.6990–18.5258% at 5–60 minutes.
- Detectable surprise-response association **remains** after subtracting the
  frozen expected-response baseline at 1, 5 and 15 minutes (secondary Holm).
- At 30 and 60 minutes, the residual is not detectable after correction.

**Core CPI m/m.**

- The same pattern holds. Residual effective GCMI is **27.8406%** of matched
  raw at 1 minute and 4.8669–14.9428% at 5–60 minutes.
- Detectable association remains at 1 and 5 minutes (secondary Holm), and not
  at 15, 30 or 60 minutes.
- This panel overlaps CPI m/m in 75 of 77 releases and shares their SPY
  responses, so it is not an independent replication.

**NFP.** No detectable residual GCMI association is found for NFP under the
frozen secondary procedure. The matched-raw GCMI profile on the same 97
releases is also not detectable.

**Benchmarks.** For the CPI-family residuals, Pearson r is −0.23 to −0.30
at 15–60 minutes while Spearman ρ is small and not significant. For
NFP residuals, Pearson and Spearman differ in sign. These divergences were
not investigated in this experiment and are not interpreted.

**Profile language.** These are information profiles over horizon. No decay
model, half-life or monotonic fit was estimated, and no decay test was
pre-specified.

## 10. Null B (pipeline-aware residual null)

The residual is built with a baseline that depends on the surprise, so the
permutation null must rebuild it. For each of the 10,000 permutations, one
draw is shared by all five horizons:

1. Permute the surprise **label** `(surprise_std, surprise_raw,
   surprise_sign)` at release level across the family's label pool (valid
   standardized surprise), within the chronological strata. Burn-in
   releases keep their own signs.
2. Recompute the frozen sign-conditioned walk-forward expected response:
   strictly earlier usable responses of the same permuted raw-sign group, per
   horizon, minimum history 8.
3. Recompute the residuals and the residual cohort under the permutation.
   The permuted cohort size was 77 for CPI and Core CPI, and 97–98 for NFP.
4. Apply tie policy B and compute GCMI.

The observed statistic is the same function at the identity permutation.
**Null A (fixed residual) was not used.** It is invalid for residual
inference (Amendment 02 §1.2) and is not reported here.

**Baseline replica equivalence, verified inside the run:**

- At the identity permutation, the vectorized replica reproduced the stored
  v2 expected and residual values exactly (maximum absolute difference 0.0),
  together with their definedness, the residual cohort and the residual
  ranks.
- On 25 permuted real frames per family, it equalled the frozen
  `src/research/baseline.add_baseline` exactly (maximum absolute difference
  0.0, 125 horizon checks per family).

## 11. Baseline sign-conditioning caveat

The frozen expected response conditions on the **sign of the raw surprise**.
Residualization is therefore **not independent of S**, which is precisely why
the pipeline-aware Null B is required. `I(S; E_h)` is baseline-adjusted
dependence:

- it is **not** `I(S; R_h | market information)`;
- it is **not** conditional mutual information;
- it does **not** represent information unknown to the market.

Its value depends on the specific frozen baseline: an expanding mean of the
same family, symbol and sign group, with a minimum history of 8.

## 12. 60-minute session-boundary caveat

All releases are at 08:30 ET. The 60-minute window ends at the 09:30
regular-session open, so the 60-minute point is
**session-boundary-adjacent**. A change between 30 and 60 minutes is not
interpreted as pure information absorption.

## 13. Limitations

- **Cohort size and composition.** The residual cohorts are smaller than the
  primary ones (77 / 97 vs 93 / 103). The baseline's minimum-history rule
  removes the earliest releases of each sign group, so the development
  stratum is reduced: 11 for the CPI families.
- **Dependent panels.** CPI and Core CPI overlap heavily.
- **Estimator scope.** GCMI captures primarily monotonic dependence; KSG has
  not been run.
- **Baseline dependence.** The result is specific to the frozen
  sign-conditioned expanding-mean baseline. No alternative baseline was
  examined.
- **Interval coverage.** Intervals are nominal 95% with about 0.93 synthetic
  coverage. They are wide.
- **p-value resolution.** The smallest attainable p is 1/10,001.
- **No ratio uncertainty.** The residual / matched-raw ratios have no
  uncertainty quantification.
- **Retrospective historical inference.** This is not prospective
  validation.
- **Surprise measurement.** Forecasts are not independently verified as
  point-in-time.

## 14. Output hashes

Outputs are in `data/reports/information_decay/residual_spy_v1/`, combined
sha256 **`2216d6c7f3a2234fbb3d37d8643e4fa52ab1332e0d5b1bad5171960bbe802fc4`**.

| File | sha256 |
|---|---|
| `cohort_release_ids.csv` | `edbc6c3287438653e69a89c5fa8f9fef8bc30e2d1f8900db04ee0e11779c65f1` |
| `matched_raw_profile.csv` | `29997f0ea9363dbe449aee1ef3594c1d8f291c3125dc3f9d1d9ba5944e660520` |
| `matched_vs_residual.csv` | `1e833967279310fdfc8d495ad684b05aedd5e65ee17c91d51bf3e95d0a2d4815` |
| `plot1_matched_raw_vs_residual.svg` | `0d624a86efa01a935889a722c698da6163adbcca1fb52f6249c1b6e8d7aefeba` |
| `plot2_residual_information_profile.svg` | `26952879517b5efcbedc58a0c699bb09e4a291c979ee71430d3a09f8c3c56ec8` |
| `plot3_residual_correlation_benchmarks.svg` | `0b25c52cdcd3be51a8a4ae5baa98c3420e1bf25fa1e2971766759945628a39bf` |
| `residual_family_holm.csv` | `9cf28e74e538ea37e909f77494f1f660dd614610b19710d7e1d35519aec52ae0` |
| `residual_profile.csv` | `aa11994a443ca315956dd42ca93504c7eaa9df5f1113eaa5178bd8d3479ef3b3` |
| `summary.json` | `fc6aa89ebd146c0d912e816a251b5c119d33a6016d6cf74d64962f959ed26a25` |

An independent rerun reproduced every output byte for byte.

## 15. Independent review

The independent review verdict is **`SECONDARY SPY RESIDUAL RESULTS
ACCEPTED`**. These results are frozen as tag
`m3-secondary-spy-residual-profile-v1`, before any QQQ or KSG analysis.
