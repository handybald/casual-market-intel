# M3 Information Decay v1 — Primary SPY Information Profile: Result Record

**Status: ACCEPTED by independent review** (`PRIMARY M3 RESULTS ACCEPTED`).
Frozen before any secondary analysis: QQQ, residual MI, KSG, other families
and decay fitting have not been run.

This record is **descriptive only**. It introduces no methodology. The
protocol is the frozen methodology plus Amendments 02–04. Machine-readable
results are in `data/reports/information_decay/primary_v1/`, and provenance is
in `metadata/research/information_decay_primary_v1.json`.

## 1. Identity and provenance

| Item | Value |
|---|---|
| Calibration-freeze tag | `m3-information-decay-calibration-freeze` → commit `f08fe850c142fed595e847ce03d6931908587c6d` |
| Protocol commits | methodology `d976c3e408ec770ffa6633af22b422d3814e0e16`; Amendment 02 `e18d46e1110753923df6230ec388ca453b9ecd4e`; Amendment 03 `cef704ed34d971487ddcc5e9525da2ca1735e77a`; Amendment 04 `9f4bef157cd490f848cb3544ab92475def430edc` |
| Primary runner | `scripts/run_information_decay_primary_v1.py`, added in `36ac538209c4b990c021faf3153e9ae0a03ca340`. The run used HEAD **`0a388345a8953e542cf271d4cd65b1c164362f78`**, which differs only by a plot-footnote layout fix. The frozen library `src/research/information_decay` is byte-identical to the freeze tag. |
| Implementation fingerprint | `88342f5a54b8c914320e6d148cfecf01779c2d271066c0874345f3ee1732d4c8` (25 files) |
| Run config | `config/information_decay_primary_v1.yaml`, sha256 `046a4fa397fcc223dd4d4afbcad6e2c8b69dbc7a964c865a8e98060c48b98531` |
| Input dataset | `data/processed/event_response/event_response_v2.parquet`: content sha256 `a6f83a05be0699032dba64e1d712d8d5d6f132dcdf154db8e890fd571c049986` (matches its recorded value; 2,472 rows), file sha256 `5bbdb16dc7256763d80c7bef39ae82be83ce7619f4cdee0091410e80364f3e03` |
| Market dataset | `sha256:b4c6573e873774053183ecbe9a8a981431926795b8bcaaa9b6c440e7b654b1d7` (Alpaca SIP 1-minute) |
| Run timestamp | 2026-10-07T23:15:50Z |
| Working tree | No tracked file modified and no scientific file untracked. Untracked prose (`…amendment_03_results.md`) is recorded in the metadata. |

## 2. Scope and estimator

| Item | Value |
|---|---|
| Market | SPY |
| Families | CPI m/m, Core CPI m/m, NFP |
| Response | raw `R_h` |
| Horizons | h ∈ {1, 5, 15, 30, 60} minutes |
| Cohort | common-support `cs_raw` |
| Estimator | GCMI, tie policy B |

The estimator is **permutation-null-adjusted Gaussian-copula dependence in
bits**. It is primarily sensitive to monotonic dependence and is **not** a
universal nonlinear MI estimator.

Inference:

- 10,000 permutations within the chronological strata (2016–2020,
  2021–2022, 2023–2026-09), with the draw shared across horizons;
- p = (1 + count)/(B + 1);
- a horizon max-statistic within each family;
- Holm across the three family panels;
- a 2,000-replicate stratified release-level percentile bootstrap.

## 3. Cohorts

| Family | n | Release-ID sha256 | Cohort id | dev / val / test |
|---|---|---|---|---|
| CPI m/m | **93** | `17131c2dd66ffdcc6c04324db775b07cd26fbd6e1d39f983e1f9078b67060bb7` | `17131c2dd66ffdcc` | 27 / 23 / 43 |
| Core CPI m/m | **93** | `17131c2dd66ffdcc6c04324db775b07cd26fbd6e1d39f983e1f9078b67060bb7` | `17131c2dd66ffdcc` | 27 / 23 / 43 |
| NFP | **103** | `74bab3d4361770b607dfd6ce830f5340cb25ca4cf6dba990d581531eb4a06e94` | `74bab3d4361770b6` | 41 / 22 / 40 |

- These are the frozen sizes (methodology §7.2; Amendment 04).
- Each family has one observation per release.
- All five horizons use the same releases.
- **CPI m/m and Core CPI m/m use the same 93 releases and identical SPY
  response vectors.** They are separate panels but **not independent
  replications**.

## 4. Effective GCMI profile, significance and bootstrap intervals

All values are in bits. Effective = raw − permutation-null mean, not
truncated. p = 0.0001 is the resolution floor 1/10,001: no permutation
reached the observed statistic.

The interval is a **nominal 95% percentile bootstrap interval of effective
GCMI**. In synthetic calibration its observed coverage was about **0.93**,
not 0.95.

| Family | h (min) | n | GCMI raw | Null mean | **Effective** | 95% bootstrap (effective) | p (pointwise) | p (horizon-adj.) |
|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 93 | 0.4988 | 0.0099 | **0.4889** | [0.3236, 0.6632] | 0.0001 | 0.0001 |
| CPI m/m | 5 | 93 | 0.4515 | 0.0095 | **0.4420** | [0.2851, 0.6165] | 0.0001 | 0.0001 |
| CPI m/m | 15 | 93 | 0.3424 | 0.0090 | **0.3334** | [0.1783, 0.5184] | 0.0001 | 0.0001 |
| CPI m/m | 30 | 93 | 0.3187 | 0.0095 | **0.3092** | [0.1555, 0.5002] | 0.0001 | 0.0001 |
| CPI m/m | 60* | 93 | 0.2742 | 0.0094 | **0.2648** | [0.1232, 0.4488] | 0.0001 | 0.0001 |
| Core CPI m/m | 1 | 93 | 0.5027 | 0.0098 | **0.4929** | [0.3240, 0.6910] | 0.0001 | 0.0001 |
| Core CPI m/m | 5 | 93 | 0.4441 | 0.0096 | **0.4344** | [0.2745, 0.6231] | 0.0001 | 0.0001 |
| Core CPI m/m | 15 | 93 | 0.2969 | 0.0097 | **0.2871** | [0.1495, 0.4633] | 0.0001 | 0.0001 |
| Core CPI m/m | 30 | 93 | 0.2556 | 0.0100 | **0.2456** | [0.1197, 0.3928] | 0.0001 | 0.0001 |
| Core CPI m/m | 60* | 93 | 0.2488 | 0.0101 | **0.2387** | [0.1128, 0.3925] | 0.0001 | 0.0001 |
| NFP | 1 | 103 | 0.0204 | 0.0054 | **0.0150** | [-0.0053, 0.0591] | 0.0513 | 0.1401 |
| NFP | 5 | 103 | 0.0088 | 0.0060 | **0.0028** | [-0.0060, 0.0479] | 0.2266 | 0.4338 |
| NFP | 15 | 103 | 0.0168 | 0.0063 | **0.0106** | [-0.0062, 0.0662] | 0.0990 | 0.1979 |
| NFP | 30 | 103 | 0.0167 | 0.0054 | **0.0113** | [-0.0054, 0.0643] | 0.0753 | 0.2000 |
| NFP | 60* | 103 | 0.0201 | 0.0055 | **0.0146** | [-0.0054, 0.0669] | 0.0546 | 0.1420 |

\* Session-boundary-adjacent horizon (§8).

## 5. Family-level Holm (FWER 0.05 across the three primary panels)

| Family | Panel p (min horizon-adj.) | Holm rank | Holm threshold | Holm-adjusted p | Panel rejected | Detectable horizons |
|---|---|---|---|---|---|---|
| Core CPI m/m | 0.0000999900 | 1 | 0.016667 | 0.00029997 | **yes** | 1, 5, 15, 30, 60 |
| CPI m/m | 0.0000999900 | 2 | 0.025 | 0.00029997 | **yes** | 1, 5, 15, 30, 60 |
| NFP | 0.140086 | 3 | 0.05 | 0.140086 | **no** | none |

The CPI and Core CPI panels tie at the p floor. Holm ordering breaks the tie
by name.

## 6. Correlation benchmarks (not substitutes for GCMI)

Two-sided permutation p-values use the same permutations.
`I_lin = −½·log2(1 − r²)` uses Pearson r.

| Family | h (min) | Pearson r | p (two-sided) | Spearman ρ | p (two-sided) | I_lin (bits) | Effective GCMI |
|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | -0.571 | 0.0001 | -0.685 | 0.0001 | 0.2848 | 0.4889 |
| CPI m/m | 5 | -0.527 | 0.0004 | -0.689 | 0.0001 | 0.2343 | 0.4420 |
| CPI m/m | 15 | -0.470 | 0.0005 | -0.616 | 0.0001 | 0.1801 | 0.3334 |
| CPI m/m | 30 | -0.494 | 0.0005 | -0.606 | 0.0001 | 0.2016 | 0.3092 |
| CPI m/m | 60 | -0.472 | 0.0007 | -0.534 | 0.0001 | 0.1816 | 0.2648 |
| Core CPI m/m | 1 | -0.517 | 0.0001 | -0.700 | 0.0001 | 0.2243 | 0.4929 |
| Core CPI m/m | 5 | -0.467 | 0.0002 | -0.653 | 0.0001 | 0.1774 | 0.4344 |
| Core CPI m/m | 15 | -0.414 | 0.0019 | -0.555 | 0.0001 | 0.1354 | 0.2871 |
| Core CPI m/m | 30 | -0.427 | 0.0012 | -0.504 | 0.0001 | 0.1454 | 0.2456 |
| Core CPI m/m | 60 | -0.409 | 0.0017 | -0.486 | 0.0001 | 0.1324 | 0.2387 |
| NFP | 1 | +0.131 | 0.0566 | +0.131 | 0.1527 | 0.0126 | 0.0150 |
| NFP | 5 | +0.122 | 0.1024 | +0.088 | 0.3527 | 0.0109 | 0.0028 |
| NFP | 15 | +0.201 | 0.0073 | +0.109 | 0.2683 | 0.0298 | 0.0106 |
| NFP | 30 | +0.184 | 0.0126 | +0.120 | 0.1907 | 0.0250 | 0.0113 |
| NFP | 60 | +0.254 | 0.0055 | +0.137 | 0.1328 | 0.0479 | 0.0146 |

## 7. Interpretation (statistical dependence; no causal claim)

**CPI m/m.**

- There is strong, detectable association between the standardized surprise
  and the SPY response at **every** measured horizon. Pointwise,
  horizon-adjusted and family Holm inference all agree.
- Effective GCMI is highest at 1 minute (0.489 bits). The point estimates are
  lower at each longer horizon, down to 0.265 bits at 60 minutes.
- The association is negative: a higher-than-forecast print goes with lower
  SPY returns.
- The bootstrap intervals overlap across all horizons.

**Core CPI m/m.**

- There is strong, detectable association at **every** horizon.
- Effective GCMI is highest at 1 minute (0.493 bits). The largest drop is
  between 5 and 15 minutes; from 30 to 60 minutes the values are nearly equal
  (0.246 → 0.239 bits).
- The association is negative, and the intervals overlap across horizons.
- This panel shares its releases and responses with CPI m/m. It does **not**
  independently replicate the CPI m/m result.

**NFP.**

- After the frozen corrections there is **no detectable primary GCMI
  dependence**: horizon-adjusted p ≥ 0.140, the panel is not rejected by
  Holm, and every bootstrap interval includes 0.
- Effective GCMI is 0.003–0.015 bits with no consistent horizon pattern.
- Pearson r is nominally significant at 15, 30 and 60 minutes, but Spearman
  and GCMI are not. **Pearson and rank/GCMI measures differ; the reason for
  that divergence was not tested in this experiment.**

**Benchmarks.** For the CPI families, Spearman ρ is larger in magnitude than
Pearson r, and effective GCMI exceeds `I_lin` at every horizon. Rank-based
monotonic dependence is therefore stronger than linear correlation in these
data. This is a description, not a tested mechanism.

**Information decay is not statistically established by v1.** The CPI-family
point estimates are highest at 1 minute and lower at longer horizons. But the
bootstrap intervals overlap, and no test of profile shape was part of the
frozen protocol. No decay model, half-life or decay constant was fitted. The
result is an **information profile over horizon**.

## 8. 60-minute session-boundary caveat

All primary releases are at 08:30 ET. Their 60-minute window ends at the 09:30
regular-session open, so the 60-minute point is **session-boundary-adjacent**.
A change between 30 and 60 minutes is not interpreted as pure information
absorption. No session-boundary sensitivity analysis is part of this result.

## 9. Limitations

- **One market, three families.** CPI and Core CPI are dependent (shared
  releases and responses), so there are effectively two distinct release
  panels: CPI-day releases (n = 93) and NFP-day releases (n = 103).
- **Estimator scope.** GCMI captures primarily monotonic (Gaussian-copula)
  dependence. Non-monotonic or heteroskedastic dependence would be
  understated. The KSG sensitivity family has not yet been run.
- **Interval coverage.** The intervals are nominal 95% with about 0.93
  synthetic coverage. They are wide, about ±0.17 bits for the CPI families.
- **p-value resolution.** With 10,000 permutations the smallest attainable p
  is 1/10,001.
- **Retrospective historical inference.** The development, validation and
  test labels are used only as permutation and bootstrap strata. This is not
  out-of-sample prediction or prospective validation.
- **Surprise measurement.** Forecasts come from retrospectively saved
  calendar pages, and none is independently verified as point-in-time. The
  measured dependence is with *this* surprise measure.
- **Selection.** Common support requires complete pre-market minute coverage,
  so the cohorts lean toward liquid, later-year releases.
- **Not yet examined.** The raw response is not baseline-adjusted (the
  residual analysis is secondary and not yet run), and there is no QQQ
  replication yet.

## 10. Output hashes

Outputs are in `data/reports/information_decay/primary_v1/`, combined sha256
**`31a05df0260b5f907fd79dcad611f41d6450feb02c4c3ca1e74db94dac649b60`**.

| File | sha256 |
|---|---|
| `primary_profile.csv` | `d997fbd3dc72901b97582173fc54a81ec671b7a748ba81c98d8482aa5debe579` |
| `holm_family.csv` | `7f8918f540878789984b642f06925609893fdfa947ff1460b7e0fdfbeb607109` |
| `cohort_release_ids.csv` | `9936816c873cdfe74c250c436f790c347f5b7221902e5015244da4be7eb37399` |
| `summary.json` | `fae7807d57e7a15267ea38001b6afc66af488e0b06fe284dd133ab8146567cf6` |
| `plot1_effective_gcmi.svg` | `95d865e44018a35b2c62fecfb8a60fd9b1ac191439e82ea12a43131bca90071c` |
| `plot2_raw_gcmi_and_null.svg` | `f0c1c4588ec653cba00eab5218e1bf049962d892bdff9e49c253451d41e6c118` |
| `plot3_correlation_benchmarks.svg` | `0647763faafd5e0437258af35c21434d98ba4a9b13463bd0cca5e189d349c000` |

An independent rerun reproduced every numeric output byte for byte.

## 11. Independent review

The independent review verdict is **`PRIMARY M3 RESULTS ACCEPTED`**. These
results are frozen as the M3 v1 primary milestone, tag
`m3-primary-spy-information-profile-v1`, before any secondary analysis.
