# M3 Integrated Research Report: Macro-Surprise / Equity-Response Information Profiles

| Field | Value |
|---|---|
| Scope | M3 information-decay experiment v1: frozen methodology, synthetic calibration, primary SPY result, secondary SPY residual result, QQQ cross-asset replication, and KSG estimator sensitivity |
| Status | Integrated synthesis of **frozen, independently accepted** results. **No new analysis**: every number below is read from frozen result documents, metadata or machine-readable outputs. |
| Latest milestone | `m3-ksg-sensitivity-v1` → `7c1a8f747459059939205c01e59bc1005c3293c6` |

> **Reading guide.** "Detectable" always means *detected under the frozen
> permutation procedure with the stated multiplicity correction*. GCMI is
> "permutation-null-adjusted Gaussian-copula dependence in bits", primarily
> sensitive to monotonic association. No result in this report is causal,
> and none establishes statistical information decay.

---

## 1. Executive summary

**Question.** How much statistical dependence links a standardized public
macroeconomic surprise `S` to the subsequent equity response, and how does
that dependence look across horizons of 1, 5, 15, 30 and 60 minutes after
the release?

The response `R_h` is the **cumulative** simple return from the pre-release
reference close through the end of horizon h. The windows therefore
overlap, and every profile is an association between the surprise and
cumulative responses. It is **not** a measure of information remaining, or
not yet incorporated into prices, at time h (§2).

**Data.**

| Item | Detail |
|---|---|
| Market data | Alpaca SIP consolidated 1-minute bars for SPY and QQQ, 2016–2026 |
| Macro events | Forex Factory events (actual, forecast); retrospectively saved calendar pages |
| Event families | CPI m/m, Core CPI m/m, NFP; all releases at 08:30 ET |
| Primary cohorts | SPY common support: 93 / 93 / 103 releases |

**Methods.**

- **Estimator:** a frozen primary estimator, GCMI with tie policy B.
- **Inference:** stratified release-level permutation, a horizon
  max-statistic, Holm across the three family panels, and a stratified
  percentile bootstrap.
- **Validation:** every inference rule was frozen and then validated by
  synthetic calibration *before* any real-data MI was computed.
- **Secondary analyses:**
  - a baseline-adjusted (residual) SPY profile using a pipeline-aware
    permutation null (Null B);
  - a QQQ cross-asset replication with a matched-cohort SPY comparison;
  - a KSG estimator sensitivity analysis (k = 3, 5, 10).

**Major findings.**

1. **CPI-family dependence is detectable.**
   - In SPY, both CPI m/m and Core CPI m/m show detectable dependence at
     every measured horizon (primary Holm-adjusted p = 0.0003). Effective
     GCMI is highest at 1 minute (0.4889 and 0.4929 bits) and lower at
     longer horizons (0.2648 and 0.2387 bits at 60 minutes).
   - The same holds in QQQ (replication Holm-adjusted p = 0.0003).
2. **No NFP dependence is detected.** No horizon-adjusted NFP dependence
   was detected in either asset, under GCMI or KSG.
3. **Residualization changes the profile substantially.** On identical
   releases, subtracting the frozen sign-conditioned walk-forward expected
   response leaves:
   - CPI m/m: 35.4374% of the matched-raw effective GCMI at 1 minute;
   - Core CPI m/m: 27.8406%.

   Detectable residual association remains only at short horizons: CPI
   m/m at 1, 5 and 15 minutes; Core CPI m/m at 1 and 5 minutes.
4. **Much of the SPY–QQQ gap tracks cohort composition.** Much of the
   apparent full-cohort SPY–QQQ difference is associated with cohort
   composition, and matched-cohort profiles are broadly similar.
5. **Panel-level CPI-family detection is robust to the estimator; horizon
   detection and profile shape are not.** KSG is sensitivity-only.
   - **Panel level:** all four CPI-family symbol/family panels have at least
     one detectable horizon for every tested KSG k (12 of 12 panel/k
     combinations).
   - **Horizon level:** detection differs between GCMI and KSG. Of the 20
     CPI-family cells, 13 are detected by both for every k, 6 by KSG for
     only some k, and 1 by GCMI only.
   - **Profile shape:** the shapes differ. KSG does not reproduce GCMI's
     1-minute peak, and effective KSG for CPI m/m peaks at 30 minutes in
     both assets for every k.
   - **NFP:** no horizon-adjusted detection under either estimator. There
     is one pointwise KSG exception (SPY, k = 10, 60 min, pointwise
     p = 0.00869913), which does not survive the horizon correction.

**Major limitations.**

- This is retrospective historical inference with forecast values that are
  not point-in-time verified.
- CPI and Core CPI panels are dependent, and SPY and QQQ respond to the
  same releases.
- QQQ has reduced pre-market coverage.
- The 60-minute point is session-boundary-adjacent.
- KSG null-mean imbalance (D8) affects horizon-level KSG interpretation.
- There is no causal identification and no out-of-sample validation.
- **Statistical information decay is not established.**

---

## 2. Research motivation

Scheduled macro releases publish a public macroeconomic surprise at a known
instant. That makes them a natural setting for describing how the
statistical association between the surprise and the equity response looks
at increasing horizons. No exogeneity of the surprise, and no causal
identification, is established or assumed by the inference.

**What is measured.** The response is the cumulative simple return from the
pre-release reference close (the close of the bar before the release
minute) through the end of horizon h:

`R_h = close(end of horizon h) / reference close − 1`, for
h ∈ {1, 5, 15, 30, 60} minutes.

The windows **overlap**. The first-minute price movement is part of every
later `R_h`, so the horizons are not separate time slices. `I(S; R_h)` is
therefore the association between the surprise and the **cumulative**
response at horizon h. It does **not** directly measure:

- information remaining at time h;
- information not yet incorporated into prices;
- predictive information about returns after h;
- information freshness;
- value of information;
- a decay rate.

The experiment does not measure the amount of unabsorbed information.

The quantity estimated here is an **empirical description of a
surprise–cumulative-response association profile**. Questions about
information freshness, Age-of-Information and Age-of-Incorrect-Information
analogues, and value of information need further definitions that go
beyond what is measured. Examples are incremental (non-overlapping) response
windows, conditioning on information available at h, or an explicit
decision problem. Knowing whether, and at which horizons, the surprise is
statistically associated with the cumulative response is one input to
posing such questions.

**The present work does not establish any Age-of-Information quantity,
information-freshness metric, decision value or trading value.** It provides
frozen, reproducible dependence measurements on which such questions can
later be posed (§12).

---

## 3. Data and provenance

**Market data.**

- Alpaca SIP (consolidated tape) 1-minute OHLCV, raw (unadjusted), with bar
  timestamps at the interval start, for **SPY** and **QQQ**.
- Coverage: 2016-01-04 to 2026-10-02, 2,703 expected and represented NYSE
  sessions per symbol.
- Frozen market-dataset fingerprint:
  `sha256:b4c6573e873774053183ecbe9a8a981431926795b8bcaaa9b6c440e7b654b1d7`.
- Market data after 2026-09-30 is provisional and excluded (research end
  2026-09-30).

**Pre-market coverage.**

- A response window enters only if **every** minute bar exists. A window
  with a non-trading minute is `insufficient_market_window`, and nothing is
  interpolated.
- QQQ's early pre-market coverage is sparser than SPY's. For CPI-family
  releases QQQ loses 33 releases to missing minutes against SPY's 20. For
  NFP the figures are 24 against 10.

**Macro events.**

- **Source:** Forex Factory actual, forecast and previous values, preserved
  from retrospectively saved historical calendar pages.
- **Volume:** 1,236 events, of which 1,227 are usable. The rest are
  excluded for ambiguous, conflicting, non-standard or unscheduled
  timestamps.
- **Verification:** no forecast is independently verified as
  point-in-time. Only 14 actuals match an official ALFRED release vintage
  (4 exact, 10 within display rounding).

**Surprise construction.**

- `surprise_raw = actual − forecast`.
- `S = surprise_std` is a causal expanding standardization per family:
  `(raw_t − mean(prior))/std(prior)`, using strictly earlier usable releases
  only, at least 12 of them, with ddof = 1.

**Release timestamps.**

- Timestamps are scheduled minutes, with no invented seconds. The release
  bar may contain seconds of pre-publication trading.
- All primary releases are at 08:30 ET.

**Input dataset.**

- `event_response_v2.parquet`, the frozen M2 layer: 2,472 event × symbol
  rows.
- Content sha256
  `a6f83a05be0699032dba64e1d712d8d5d6f132dcdf154db8e890fd571c049986`.
- File sha256
  `5bbdb16dc7256763d80c7bef39ae82be83ce7619f4cdee0091410e80364f3e03`.

**Frozen milestones.** The full list is in §13.

| Milestone | Tag | Commit |
|---|---|---|
| Methodology freeze | — | `d976c3e408ec770ffa6633af22b422d3814e0e16` |
| Calibration freeze | `m3-information-decay-calibration-freeze` | `f08fe850c142fed595e847ce03d6931908587c6d` |
| Primary SPY | `m3-primary-spy-information-profile-v1` | `70ee4c1f54cb36eb7808fb5a647e5b7f2cd2aa71` |
| Secondary SPY residual | `m3-secondary-spy-residual-profile-v1` | `eb2948bcafba8f5a2393efdcb47d81a8267ea5af` |
| QQQ replication | `m3-qqq-replication-v1` | `b1ec964da005113ba87cf2c2f089a0bfccb45c1e` |
| KSG sensitivity | `m3-ksg-sensitivity-v1` | `7c1a8f747459059939205c01e59bc1005c3293c6` |

---

## 4. Experimental methodology (as frozen)

**Protocol history.**

- The methodology was frozen at `d976c3e` and amended three times
  *before* any real-data MI:
  - **Amendment 02** (`e18d46e`): raw MI became primary, residual MI
    secondary with Null B only, the V4 gate was split into V4a/V4b, and the
    Monte Carlo rule was revised.
  - **Amendment 03** (`cef704e`): a fixed-sample intersection-union
    qualification rule.
  - **Amendment 04** (`9f4bef1`): exact primary cohort sizes.
- Amendment 01, the calibration outcome record, is an audit artifact.

**Common-support cohorts.** A release enters a family × symbol panel only if
all of the following hold:

- it has a valid standardized surprise;
- it has no event exclusion and no provisional data;
- it has a usable response window at **every** horizon {1, 5, 15, 30, 60}.

Every point of one curve therefore uses identical releases.

**Response.** `R_h` is the cumulative simple return from the pre-release
reference close through the end of horizon h (frozen M2 definition). The
five windows overlap: each longer window contains the shorter ones.
Horizon-to-horizon differences in dependence are therefore **not**
increments of information, and none of the measures below is a measure of
information remaining at h.

| Cohort | CPI m/m | Core CPI m/m | NFP |
|---|---|---|---|
| SPY raw | 93 | 93 | 103 |
| SPY residual | 77 | 77 | 97 |
| QQQ raw | 80 | 80 | 89 |

**Estimator.**

- **GCMI:** rank-based Gaussian copula, so the bivariate MI is
  `−½·log2(1 − r²)` of the normal scores, in bits.
- **Tie policy B:** average ranks within exact raw-surprise tie groups, with
  groups ordered by median `surprise_std`. It was selected by synthetic
  calibration (Amendment 02 D3) and is not reopened.

**Permutation-null adjustment.**

- Effective GCMI = observed − permutation-null mean, never truncated.
- 10,000 permutations plus the observed statistic; p = (1 + count)/(B + 1),
  so the floor is 1/10,001.

**Chronological stratification.**

- Surprises are permuted across release IDs within the strata 2016–2020,
  2021–2022 and 2023–2026-09. A stratum with fewer than 8 releases merges
  into the next.
- One permutation draw is shared across the five horizons.

**Multiplicity.**

- **Within a panel:** a horizon max-statistic (single-step max-T).
- **Across panels:** Holm across the three family panels in the primary
  family, with closed-testing horizon claims. Separate Holm families are
  used for the secondary residual panels and for the QQQ replication
  panels. They are never pooled.

**Bootstrap uncertainty.**

- A stratified release-level percentile bootstrap with 2,000 replicates,
  with tie policy B applied inside each replicate. The interval is shifted
  by the full-sample null mean.
- Reported as a **nominal 95% interval**. Cluster-aware synthetic coverage
  was about 0.93 (0.928–0.936 for ρ ≥ 0.35), not 0.95.

**Residual pipeline-aware Null B.**

- The frozen M2 baseline is an expanding mean of earlier same-symbol,
  same-family, same **raw-surprise-sign** responses, with a minimum history
  of 8.
- Because the residual therefore depends on the surprise, each permutation
  of Null B:
  1. permutes the surprise label (`surprise_std`, `surprise_raw`, sign)
     within strata;
  2. recomputes the baseline, the residuals and the residual cohort;
  3. applies tie policy B and computes GCMI.
- Fixed-residual permutation (Null A) is invalid. In synthetic calibration
  its false-positive rate was about 0.10–0.12 under heavy-tailed responses.

**Calibration and qualification.**

| Step | Outcome |
|---|---|
| Amendment 01 rules | Initially blocked real-data MI |
| Amendment 02 rule (strict simultaneous Holm–Wilson) | 38/42 hard cells PASS, 4 INCONCLUSIVE, 0 FAIL |
| Amendment-02 rule operating characteristic | A perfectly calibrated test qualifies only about 22–24% of the time |
| Amendment 03 rule (fixed N = 10,000, one-sided 95% Wilson bound, intersection-union) | 36/36 required cells PASS; operating characteristic about 0.862 |
| Amendment 04 exact primary sizes n = 93 and 103 | 12/12 PASS. The closest cell was V2 at n = 103, with 555 false positives against a cutoff of 558. |
| Residual Null B | Validated 12/12 |

**KSG sensitivity-only status.**

- KSG algorithm 1, max-norm, at k = 3, 5, 10, using tie policy K-A
  (`surprise_std` as given) and seeded 1e-10 tie jitter.
- It uses the same permutation draws as GCMI and a raw-MI max-statistic
  (D8).
- No confidence intervals (D7), no Holm, and no primary role (Amendment 02
  §6).

---

## 5. Primary SPY findings (`m3-primary-spy-information-profile-v1`)

| Family | h (min) | n | Effective GCMI (bits) | Nominal 95% bootstrap | p (horizon-adj.) | Detectable after primary Holm |
|---|---|---|---|---|---|---|
| CPI m/m | 1 | 93 | 0.4889 | [0.3236, 0.6632] | 0.0001 | yes |
| CPI m/m | 5 | 93 | 0.4420 | [0.2851, 0.6165] | 0.0001 | yes |
| CPI m/m | 15 | 93 | 0.3334 | [0.1783, 0.5184] | 0.0001 | yes |
| CPI m/m | 30 | 93 | 0.3092 | [0.1555, 0.5002] | 0.0001 | yes |
| CPI m/m | 60* | 93 | 0.2648 | [0.1232, 0.4488] | 0.0001 | yes |
| Core CPI m/m | 1 | 93 | 0.4929 | [0.3240, 0.6910] | 0.0001 | yes |
| Core CPI m/m | 5 | 93 | 0.4344 | [0.2745, 0.6231] | 0.0001 | yes |
| Core CPI m/m | 15 | 93 | 0.2871 | [0.1495, 0.4633] | 0.0001 | yes |
| Core CPI m/m | 30 | 93 | 0.2456 | [0.1197, 0.3928] | 0.0001 | yes |
| Core CPI m/m | 60* | 93 | 0.2387 | [0.1128, 0.3925] | 0.0001 | yes |
| NFP | 1 | 103 | 0.0150 | [-0.0053, 0.0591] | 0.1401 | no |
| NFP | 5 | 103 | 0.0028 | [-0.0060, 0.0479] | 0.4338 | no |
| NFP | 15 | 103 | 0.0106 | [-0.0062, 0.0662] | 0.1979 | no |
| NFP | 30 | 103 | 0.0113 | [-0.0054, 0.0643] | 0.2000 | no |
| NFP | 60* | 103 | 0.0146 | [-0.0054, 0.0669] | 0.1420 | no |

\* 60 min is session-boundary-adjacent. p = 0.0001 is the floor 1/10,001.

**Primary family Holm (FWER 0.05):**

| Family | Holm-adjusted p | Panel | Detectable horizons |
|---|---|---|---|
| CPI m/m | 0.000300 | rejected | 1, 5, 15, 30, 60 |
| Core CPI m/m | 0.000300 | rejected | 1, 5, 15, 30, 60 |
| NFP | 0.140086 | not rejected | none |

**Statistical detection** (evidence):

- CPI m/m and Core CPI m/m: detectable at all five horizons.
- NFP: not detectable; the panel minimum horizon-adjusted p is 0.1401.
- CPI and Core CPI share the same 93 releases and SPY responses, so they are
  **not independent replications**.

**Information-profile shape** (description only):

- CPI-family point estimates are highest at 1 minute and lower at longer
  horizons.
- The bootstrap intervals overlap across horizons, and no shape test was
  pre-specified.
- The shape is therefore **descriptive only**. It is not statistical decay.

---

## 6. Residual SPY findings (`m3-secondary-spy-residual-profile-v1`)

**Matched-cohort comparison.** The matched raw and residual profiles use the
**same** residual-cohort releases. The ratio is descriptive.

| Family | h (min) | n | Matched-raw effective | Residual effective | Residual / matched raw | Residual p (horizon-adj., Null B) | Residual nominal 95% bootstrap | Detectable after secondary Holm |
|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 77 | 0.5265 | 0.1866 | 35.4374% | 0.0001 | [0.0595, 0.3583] | yes |
| CPI m/m | 5 | 77 | 0.4569 | 0.0846 | 18.5258% | 0.0002 | [0.0065, 0.2085] | yes |
| CPI m/m | 15 | 77 | 0.3522 | 0.0383 | 10.8629% | 0.0123 | [-0.0048, 0.1431] | yes |
| CPI m/m | 30 | 77 | 0.3169 | 0.0269 | 8.4875% | 0.0381 | [-0.0047, 0.1237] | no |
| CPI m/m | 60* | 77 | 0.2942 | 0.0227 | 7.6990% | 0.0587 | [-0.0047, 0.1162] | no |
| Core CPI m/m | 1 | 77 | 0.5121 | 0.1426 | 27.8406% | 0.0001 | [0.0380, 0.3002] | yes |
| Core CPI m/m | 5 | 77 | 0.4532 | 0.0677 | 14.9428% | 0.0008 | [0.0019, 0.1877] | yes |
| Core CPI m/m | 15 | 77 | 0.3015 | 0.0334 | 11.0623% | 0.0260 | [-0.0054, 0.1359] | no |
| Core CPI m/m | 30 | 77 | 0.2559 | 0.0294 | 11.4867% | 0.0376 | [-0.0053, 0.1278] | no |
| Core CPI m/m | 60* | 77 | 0.2638 | 0.0128 | 4.8669% | 0.1695 | [-0.0052, 0.0979] | no |
| NFP | 1 | 97 | 0.0058 | -0.0074 | not meaningful | 0.9746 | [-0.0082, 0.0272] | no |
| NFP | 5 | 97 | -0.0026 | -0.0029 | not meaningful | 0.6674 | [-0.0083, 0.0527] | no |
| NFP | 15 | 97 | 0.0017 | -0.0029 | not meaningful | 0.8234 | [-0.0059, 0.0465] | no |
| NFP | 30 | 97 | 0.0027 | -0.0033 | not meaningful | 0.9883 | [-0.0039, 0.0288] | no |
| NFP | 60* | 97 | 0.0062 | -0.0032 | not meaningful | 0.9898 | [-0.0037, 0.0323] | no |

**Secondary residual family-level correction (Holm):**

| Family | Holm-adjusted p | Panel | Detectable horizons |
|---|---|---|---|
| CPI m/m | 0.000300 | rejected | 1, 5, 15 |
| Core CPI m/m | 0.000300 | rejected | 1, 5 |
| NFP | 0.667433 | not rejected | none |

**Evidence.**

- A substantial portion of the raw CPI-family surprise-response association
  is not present after subtracting the frozen sign-conditioned walk-forward
  expected response.
- Detectable residual association remains at:
  - CPI m/m: 1, 5 and 15 minutes;
  - Core CPI m/m: 1 and 5 minutes.
- NFP: no detectable residual GCMI association.

**Why the difference is not a decomposition.**

- Mutual information is not additive across a baseline subtraction.
- `I(S; R_h) − I(S; E_h)` is therefore **not** an "explained information"
  share, and the residual/matched-raw ratio is not a fraction of information
  explained.
- No delta-specific test was frozen.

**Baseline sign-conditioning.**

- The baseline conditions on the sign of the raw surprise, so the residual
  is **not** constructed independently of S. That is why only the
  pipeline-aware Null B is valid.
- `I(S; E_h)` is **baseline-adjusted dependence**:
  - it is **not** conditional MI;
  - it is **not** `I(S; R_h | market information)`;
  - it is **not** "information unknown to the market".
- The methodology's pre-registered analytic note (§2.1) already anticipated
  that sign-mean subtraction removes much of a monotonic signal by
  construction.

**Cohort note.** The CPI m/m and Core CPI m/m residual cohorts differ in 2
releases each, because each family's baseline groups by its own surprise
sign. They share 75 releases.

---

## 7. QQQ replication (`m3-qqq-replication-v1`)

| Family | h (min) | QQQ n | QQQ effective | QQQ p (horizon-adj.) | QQQ nominal 95% bootstrap | Frozen SPY effective (full cohort) | Matched n | SPY-on-intersection | QQQ-on-intersection | Matched QQQ − SPY |
|---|---|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 80 | 0.5438 | 0.0001 | [0.3650, 0.7275] | 0.4889 | 80 | 0.5517 | 0.5438 | -0.0079 |
| CPI m/m | 5 | 80 | 0.5018 | 0.0001 | [0.3225, 0.6948] | 0.4420 | 80 | 0.4841 | 0.5018 | +0.0177 |
| CPI m/m | 15 | 80 | 0.3963 | 0.0001 | [0.2285, 0.6010] | 0.3334 | 80 | 0.3502 | 0.3963 | +0.0461 |
| CPI m/m | 30 | 80 | 0.3623 | 0.0001 | [0.1913, 0.5726] | 0.3092 | 80 | 0.3415 | 0.3623 | +0.0208 |
| CPI m/m | 60* | 80 | 0.3167 | 0.0001 | [0.1518, 0.5201] | 0.2648 | 80 | 0.2990 | 0.3167 | +0.0177 |
| Core CPI m/m | 1 | 80 | 0.5727 | 0.0001 | [0.3926, 0.7646] | 0.4929 | 80 | 0.5912 | 0.5727 | -0.0184 |
| Core CPI m/m | 5 | 80 | 0.5363 | 0.0001 | [0.3663, 0.7008] | 0.4344 | 80 | 0.5102 | 0.5363 | +0.0261 |
| Core CPI m/m | 15 | 80 | 0.3974 | 0.0001 | [0.2299, 0.5876] | 0.2871 | 80 | 0.3130 | 0.3974 | +0.0844 |
| Core CPI m/m | 30 | 80 | 0.3416 | 0.0001 | [0.1846, 0.5187] | 0.2456 | 80 | 0.2905 | 0.3416 | +0.0511 |
| Core CPI m/m | 60* | 80 | 0.3401 | 0.0001 | [0.1873, 0.5211] | 0.2387 | 80 | 0.2947 | 0.3401 | +0.0454 |
| NFP | 1 | 89 | -0.0020 | 0.7548 | [-0.0061, 0.0350] | 0.0150 | 85 | -0.0060 | -0.0025 | +0.0035 |
| NFP | 5 | 89 | 0.0051 | 0.3830 | [-0.0067, 0.0536] | 0.0028 | 85 | -0.0092 | 0.0039 | +0.0130 |
| NFP | 15 | 89 | 0.0009 | 0.5229 | [-0.0073, 0.0564] | 0.0106 | 85 | -0.0081 | 0.0009 | +0.0089 |
| NFP | 30 | 89 | -0.0051 | 0.9345 | [-0.0067, 0.0278] | 0.0113 | 85 | -0.0034 | -0.0054 | -0.0021 |
| NFP | 60* | 89 | -0.0072 | 1.0000 | [-0.0072, 0.0207] | 0.0146 | 85 | 0.0003 | -0.0071 | -0.0074 |

**QQQ replication family correction (Holm; never pooled with SPY):**

| Family | Holm-adjusted p | Panel | Detectable horizons |
|---|---|---|---|
| CPI m/m | 0.000300 | rejected | 1, 5, 15, 30, 60 |
| Core CPI m/m | 0.000300 | rejected | 1, 5, 15, 30, 60 |
| NFP | 0.382962 | not rejected | none |

**Evidence.**

- The CPI-family surprise-response association observed in SPY is also
  detected in QQQ under the same frozen procedure, at all five horizons.
- This is **cross-asset consistency / replication, not independent
  experimental replication**: SPY and QQQ respond to the same macro
  releases, so QQQ does not double the sample.
- NFP: no detectable GCMI association was found in either SPY primary or
  QQQ replication under their respective frozen common-support cohorts.

**Cohort composition and missing pre-market windows.**

| Family | QQQ cohort | SPY cohort | Intersection | Release split |
|---|---|---|---|---|
| CPI m/m and Core CPI m/m | 80 | 93 | 80 | 13 SPY-only releases (2017-09 to 2021-08); QQQ is a strict subset |
| NFP | 89 | 103 | 85 | 18 SPY-only and 4 QQQ-only releases |

The 4 QQQ-only NFP releases lack a complete SPY window, so the matched
comparison uses the exact 85-release intersection: 2018-09-07, 2020-11-06,
2021-09-03 and 2025-02-07 are excluded. Nothing was patched.

| Comparison | CPI m/m | Core CPI m/m |
|---|---|---|
| QQQ − SPY, full frozen cohorts | +0.052 to +0.063 bits | +0.080 to +0.110 bits |
| Matched QQQ − SPY, same releases | −0.008 to +0.046 bits | −0.018 to +0.084 bits |

Much of the apparent full-cohort difference is associated with cohort
composition; matched-cohort profiles are broadly similar.

The remaining matched Core CPI differences are 15 min **+0.0843557692**,
30 min +0.0511073372 and 60 min +0.0454221544 bits. These are descriptive
only:

- no cross-asset difference test was pre-specified;
- the intervals overlap;
- they are not evidence of structural heterogeneity.

QQQ is not described as statistically stronger than SPY.

---

## 8. KSG estimator sensitivity (`m3-ksg-sensitivity-v1`)

KSG is sensitivity-only, it is less calibrated than GCMI, and its magnitudes
are not compared with GCMI's as like quantities.

| Symbol | Family | KSG k | Horizons with horizon-adj. p ≤ 0.05 | Peak effective horizon | Panel p (min horizon-adj.) |
|---|---|---|---|---|---|
| SPY | CPI m/m | 3 | 1, 5, 30 | 30m | 0.0005 |
| SPY | CPI m/m | 5 | 1, 5, 15, 30 | 30m | 0.0002 |
| SPY | CPI m/m | 10 | 1, 5, 15, 30, 60 | 30m | 0.0001 |
| SPY | Core CPI m/m | 3 | 1, 5, 15 | 1m | 0.0001 |
| SPY | Core CPI m/m | 5 | 1, 5, 15, 30 | 1m | 0.0001 |
| SPY | Core CPI m/m | 10 | 1, 5, 15, 30 | 1m | 0.0001 |
| SPY | NFP | 3 | none | 30m | 0.5283 |
| SPY | NFP | 5 | none | 1m | 0.8819 |
| SPY | NFP | 10 | none | 60m | 0.1006 |
| QQQ | CPI m/m | 3 | 1, 5, 15, 30, 60 | 30m | 0.0002 |
| QQQ | CPI m/m | 5 | 1, 5, 15, 30, 60 | 30m | 0.0002 |
| QQQ | CPI m/m | 10 | 1, 5, 15, 30, 60 | 30m | 0.0001 |
| QQQ | Core CPI m/m | 3 | 1, 5 | 1m | 0.0003 |
| QQQ | Core CPI m/m | 5 | 1, 5, 15, 30, 60 | 5m | 0.0002 |
| QQQ | Core CPI m/m | 10 | 1, 5, 15, 30, 60 | 1m | 0.0002 |
| QQQ | NFP | 3 | none | 60m | 0.6733 |
| QQQ | NFP | 5 | none | 60m | 0.9903 |
| QQQ | NFP | 10 | none | 60m | 0.9569 |

**Corrected 30-cell agreement classification.** The cells are 2 symbols × 3
families × 5 horizons, labelled descriptively by horizon-adjusted p ≤ 0.05:

| Classification | Cells |
|---|---|
| Both GCMI and KSG, all k | **13** |
| Both, some k | **6** |
| GCMI only (SPY Core CPI m/m, 60 min) | **1** |
| KSG only | **0** |
| Neither (all NFP) | **10** |
| **Total** | **30** |

**Estimator robustness of CPI-family detection.** All four CPI-family
symbol/family panels have at least one detectable horizon for each k:
**12 of 12** panel/k combinations. Not every horizon is detected.

**Failure to reproduce GCMI's horizon shape.**

- GCMI CPI-family effective estimates peak at 1 minute in both assets.
- Effective KSG for **CPI m/m peaks at 30 minutes in SPY and QQQ for every
  k**.
- Core CPI m/m peaks at 1 minute in SPY for every k; in QQQ at 1 minute for
  k = 3 and k = 10, and at 5 minutes for k = 5.
- **The presence of CPI-family dependence is qualitatively robust across
  estimators, but the temporal information profile is not.**

**D8 null-mean imbalance.**

| Diagnostic | Threshold | Exceeded in |
|---|---|---|
| A = (max − min null mean) / mean null SD | > 0.20 | **14 of 18** symbol/family/k combinations |
| B = max/min null SD | > 1.20 | **0 of 18** |

- The frozen raw-MI max-statistic was applied as specified, with no
  studentization.
- Horizon-adjusted KSG p-values are therefore not equal-power comparisons.
- Panel-level inference rests on the stratified exchangeability
  assumption.
- Strong horizon-wise FWER under partial alternatives was not established.

**No calibrated KSG confidence intervals** (D7).

**NFP pointwise exception.** SPY NFP, k = 10, 60 minutes:

| Quantity | Value |
|---|---|
| Observed | 0.035518 bits |
| Null mean | −0.025834 bits |
| Pointwise p | 0.00869913 |
| Horizon-adjusted p | 0.10058994 |

It does not survive the frozen horizon correction, so there is no
detectable NFP KSG dependence under the frozen horizon-adjusted procedure.
Not every pointwise NFP test was non-significant.

---

## 9. Integrated scientific findings

### A. Supported conclusions (frozen inference; within stated limitations)

1. CPI m/m and Core CPI m/m surprises show detectable statistical dependence
   with the SPY raw response at all five horizons (primary Holm), and with
   the QQQ raw response at all five horizons (replication Holm).
2. No horizon-adjusted dependence was detected for NFP in SPY or QQQ, under
   GCMI or KSG.
3. After subtracting the frozen sign-conditioned baseline, detectable
   residual CPI-family association remains at short horizons:
   - CPI m/m: 1, 5 and 15 minutes;
   - Core CPI m/m: 1 and 5 minutes.
4. CPI-family detection is robust to the estimator: KSG detects it in
   12/12 panel/k combinations.

### B. Descriptive observations (no inferential claim)

- GCMI CPI-family point estimates are highest at 1 minute and lower at
  longer horizons, with overlapping intervals.
- On identical releases, residual effective GCMI is 35.4374% (CPI m/m) and
  27.8406% (Core CPI m/m) of matched raw at 1 minute, and 4.8669–18.5258%
  at 5–60 minutes.
- Full-cohort QQQ − SPY differences shrink on matched releases. Core CPI
  matched differences of +0.045 to +0.084 bits remain at 15–60 minutes.
- KSG CPI m/m effective estimates peak at 30 minutes, not 1 minute.
- Spearman magnitudes exceed Pearson for the CPI families, and effective
  GCMI exceeds the linear benchmark `I_lin`.
- For NFP, Pearson and rank/GCMI measures differ; the reason was not tested.

### C. Unsupported claims (explicitly not made)

- Statistical information decay, or any decay rate, half-life or decay
  constant.
- Causal effects of surprises on prices; "markets absorb information".
- Residual GCMI as conditional MI, or as information unknown to the market.
- NFP having no market effect.
- QQQ being statistically stronger or weaker than SPY.
- SPY and QQQ, or CPI and Core CPI, as independent replications.
- KSG confirming GCMI's horizon shape.
- Any trading, decision or value-of-information implication.

### D. Open scientific questions

- Is the horizon-profile disagreement between GCMI and KSG due to estimator
  sensitivity (KSG null-mean imbalance, near-discrete surprises,
  heavy-tailed responses) or to dependence components that GCMI does not
  capture?
- What drives the NFP Pearson-versus-rank divergence?
- How does dependence change at and after the 09:30 session boundary?
- Does dependence vary with market state (volatility, regime) or with
  surprise magnitude?
- Would conditioning on pre-release information, rather than a
  sign-conditioned baseline, change the residual picture?

---

## 10. Limitations and threats to validity

| Threat | Description |
|---|---|
| Retrospective analysis | All inference is retrospective historical inference. There is no prospective or out-of-sample validation. |
| Forecast vintages | Forecasts come from retrospectively saved pages and none is point-in-time verified. Only 14 of 1,236 actuals are cross-validated against official release vintages. |
| Exchangeability | Permutation inference assumes surprises are exchangeable across releases *within* the chronological strata. |
| Finite samples | n = 77–103 per panel; wide intervals (about ±0.17 bits for CPI-family GCMI); p-value floor 1/10,001. |
| Ties / discrete surprises | CPI-family raw surprises take 9 values. GCMI uses tie policy B; KSG uses K-A, a different representation. KSG is tie- and atom-sensitive. |
| Calibration limits | GCMI intervals have about 0.93 synthetic coverage. The Amendment-02 qualification rule was over-conservative and was superseded by Amendment 03 before reclassification. KSG has lower power and no calibrated CI. The KSG D8 null imbalance (14/18) limits horizon-level KSG interpretation. |
| Overlapping event panels | CPI m/m and Core CPI m/m share releases and responses. They are dependent panels; Holm stays valid under dependence. |
| Overlapping asset responses | SPY and QQQ respond to the same releases and are not independent samples. |
| Pre-market coverage | Complete-minute requirements exclude more early QQQ releases (and some SPY ones). Cohorts lean toward liquid, later years. |
| 60-minute session boundary | For 08:30 ET releases the 60-minute window ends at the 09:30 open. A 30→60 change is not interpreted as information absorption. |
| No causal identification | All measures are statistical dependence. |
| No predictive validation | No out-of-sample forecasting evaluation was performed. |
| No decay testing | No decay model or shape test was pre-specified or fitted. |
| Residual baseline specificity | Residual results depend on the frozen sign-conditioned expanding-mean baseline. No alternative baseline was examined. |

---

## 11. M3 conclusions

- Macro-surprise/market-response dependence is detectable for CPI-family
  events in SPY and QQQ under the frozen procedure.
- No corresponding horizon-adjusted dependence was detected for NFP under
  the tested procedures.
- Residualization substantially changes the observed dependence profile,
  with some short-horizon CPI association remaining.
- Estimator choice affects the observed temporal profile.
- Statistical information decay is not established.

All of these statements concern associations between the public
macroeconomic surprise and **cumulative** responses `R_h` over overlapping
windows. None measures information remaining or not yet incorporated at
horizon h, information freshness or value of information.

---

## 12. M4 research questions (proposals only; nothing implemented)

These are **hypotheses and questions**, not results.

1. **Conditional information measures.** Estimate `I(S; R_h | Z_pre)`, with
   `Z_pre` covering pre-release volatility, return, volume and simultaneous
   surprises, using a pre-registered conditional-MI estimator and its own
   calibration. *Hypothesis:* part of the CPI-family dependence is
   state-dependent.
2. **Information freshness.** Define an empirical information-age or AoII
   analogue, with a mathematical definition before any estimation. The
   frozen profiles cannot serve directly, because they use overlapping
   cumulative responses. A freshness measure would need, for example,
   incremental (non-overlapping) response windows or conditioning on
   information available at h. *Question:* under such a definition, is
   there a horizon beyond which the surprise is no longer statistically
   associated with the incremental response, and is that horizon
   estimator-invariant?
3. **Value of information.** Formalize a decision problem (for example,
   position sizing at horizon h) and its value-of-information functional
   before any empirical use. *Question:* does statistical dependence
   translate into decision value at any horizon?
4. **Decision-quality improvement.** Pre-specify decision metrics and
   compare decisions with and without surprise information.
   *Hypothesis:* untested.
5. **Market-state dependence.** Stratify or condition by pre-registered
   market-state variables. *Question:* does CPI-family dependence differ
   across regimes?
6. **Out-of-sample evaluation.** Use a prospective or strictly held-out
   design to evaluate any predictive use. M3 provides none.

Supporting work would include a pre-registered session-boundary analysis
across the 09:30 open, mixture-aware nearest-neighbour estimators for
near-discrete surprises, and a QQQ residual analysis with explicit
exploratory treatment of its smaller cohorts.

---

## 13. Reproducibility appendix

**Frozen tags and commits.**

| Tag / role | Commit |
|---|---|
| Methodology freeze | `d976c3e408ec770ffa6633af22b422d3814e0e16` |
| Amendment 02 | `e18d46e1110753923df6230ec388ca453b9ecd4e` |
| Amendment 03 (+ OC artifact) | `cef704ed34d971487ddcc5e9525da2ca1735e77a` |
| Amendment 04 (+ inventory) | `9f4bef157cd490f848cb3544ab92475def430edc` |
| `m3-information-decay-calibration-freeze` | `f08fe850c142fed595e847ce03d6931908587c6d` |
| Primary runner / plot fix | `36ac538209c4b990c021faf3153e9ae0a03ca340` / `0a388345a8953e542cf271d4cd65b1c164362f78` |
| `m3-primary-spy-information-profile-v1` | `70ee4c1f54cb36eb7808fb5a647e5b7f2cd2aa71` |
| Residual runner | `097b92afac72a92df9a815d16375483fd18f8c6c` |
| `m3-secondary-spy-residual-profile-v1` | `eb2948bcafba8f5a2393efdcb47d81a8267ea5af` |
| QQQ runner | `026360afc8fb7fc26937b4fb5ad4dd7c086bc91a` |
| `m3-qqq-replication-v1` | `b1ec964da005113ba87cf2c2f089a0bfccb45c1e` |
| KSG runner | `286d93c159b2c33639247be61b11845b7b52b900` |
| `m3-ksg-sensitivity-v1` | `7c1a8f747459059939205c01e59bc1005c3293c6` |

**Scientific fingerprints and output hashes** (from each milestone's
metadata):

| Analysis | Metadata | Implementation fingerprint | Combined output sha256 |
|---|---|---|---|
| Synthetic calibration (Amendment 01) | `information_decay_calibration_v1.json` | uncommitted-tree sha `5e061aa4d89f43431b764e38c96084f598ec391b22ebd334179a9fb62e1e44ad` (recorded before the freeze commit) | `c451c13fb1cee3c413e575f60ff5cbdcdf54a7c9f48150f964135351b6b8fbfc` |
| Amendment-02 revalidation | `information_decay_amendment02_revalidation.json` | `b73d94571d11812f562e39f1afc59b8256b703910836d6d5061921eab09e4b62` | `782efec1dbf0c029b618cb57c9b4c476ffbdaf096e5e2718ab842ae79da311ee` |
| Amendment-03 qualification | `information_decay_amendment03_qualification.json` | `5552d8bce45d70d7efc3d6361797f6cf04593975702138660d0f1e616d90bf6b` | `0ab9ceaef7fc76c6fe7541aaa451f28c3a49d48d2386a64049ace2f5d94f3358` |
| Amendment-04 exact sizes | `information_decay_amendment04_exact_size.json` | `82e5f46ccd41920106aa0657bbcdc4727f21a751b09fe4bec78f494e9473a1e4` | `16080b14b1db67e59a7c75b297bb51e085cf4725b1099a37c9fd62bbf5e74314` |
| Primary SPY | `information_decay_primary_v1.json` | `88342f5a54b8c914320e6d148cfecf01779c2d271066c0874345f3ee1732d4c8` | `31a05df0260b5f907fd79dcad611f41d6450feb02c4c3ca1e74db94dac649b60` |
| Residual SPY | `information_decay_residual_spy_v1.json` | `2512635e7b33f47200df766f7e5c26284aca44f0f7f4cdc3cbfb328dd6b35b65` | `2216d6c7f3a2234fbb3d37d8643e4fa52ab1332e0d5b1bad5171960bbe802fc4` |
| QQQ replication | `information_decay_qqq_replication_v1.json` | `1e19fee35ed1d592aaad7b5a14502d55251d2bfba9fd1fb70a9801960e924dc1` | `3d5e8f593f157fca8fe742e0779b781145a4e9ea2e28a6ee8bc34f8616ae8932` |
| KSG sensitivity | `information_decay_ksg_sensitivity_v1.json` | `0a1e00e447c92073ec7639789f97e37e9af131b21bfc1376b2cb8b9a629ce538` | `c93846f4c6dd9d7c408120d762dbb91a38128080eada2c3fad5a88e7c36b1d6d` |

The metadata files are under `metadata/research/`. Implementation
fingerprints from Amendment 04 onward are pattern-based:
`src/research/information_decay/**/*.py`, `scripts/*information_decay*.py`,
`config/information_decay*.yaml`.

**Scripts and configs.**

| Script | Config | Notes |
|---|---|---|
| `scripts/calibrate_information_decay_estimators.py` | `config/information_decay_v1.yaml` | |
| `scripts/revalidate_information_decay_amendment02.py` | `config/information_decay_v1_amendment02.yaml` | |
| `scripts/qualify_information_decay_amendment03.py` | `config/information_decay_v1_amendment03.yaml` | |
| `scripts/qualify_information_decay_amendment04.py` | `config/information_decay_v1_amendment03.yaml` | Uses the committed inventory `docs/research/information_decay_v1_amendment_04_inventory.json` |
| `scripts/run_information_decay_primary_v1.py` | `config/information_decay_primary_v1.yaml` | |
| `scripts/run_information_decay_residual_spy_v1.py` | `config/information_decay_residual_spy_v1.yaml` | |
| `scripts/run_information_decay_qqq_replication_v1.py` | `config/information_decay_qqq_replication_v1.yaml` | |
| `scripts/run_information_decay_ksg_sensitivity_v1.py` | `config/information_decay_ksg_sensitivity_v1.yaml` | |

The frozen library is `src/research/information_decay/` (estimators,
inference, seeds, isolation guard, provenance). The frozen M2 baseline is
`src/research/baseline.py`.

**Commands used** (from the repository root):

```bash
.venv/bin/python -B scripts/calibrate_information_decay_estimators.py --profile final --workers 8
.venv/bin/python -B scripts/revalidate_information_decay_amendment02.py --workers 8
.venv/bin/python -B scripts/qualify_information_decay_amendment03.py --workers 8
.venv/bin/python -B scripts/qualify_information_decay_amendment04.py --workers 8
.venv/bin/python -B scripts/run_information_decay_primary_v1.py
.venv/bin/python -B scripts/run_information_decay_residual_spy_v1.py
.venv/bin/python -B scripts/run_information_decay_qqq_replication_v1.py
.venv/bin/python -B scripts/run_information_decay_ksg_sensitivity_v1.py
.venv/bin/python -B -m pytest -q -p no:cacheprovider
```

**A. Checks enforced automatically by each runner.** The runners have
different gates; each refuses to run, or stops, if its own checks fail. The
entries below are taken from the runner source code.

| Runner | Automatic checks (refuse to run or STOP otherwise) | Not checked by this runner |
|---|---|---|
| `calibrate_information_decay_estimators.py` (synthetic) | Methodology freeze commit exists and the methodology document is unchanged since it; the `final` profile runs every cell; runs inside the real-data isolation guard | No real data; no prior outputs |
| `revalidate_information_decay_amendment02.py` (synthetic) | Amendment 02 and the methodology unchanged since their commits; stored Amendment-01 outputs match their recorded hashes; isolation guard | No real data |
| `qualify_information_decay_amendment03.py` (synthetic) | Amendments 02–03, the Amendment-03 operating-characteristic file and the methodology unchanged since their commits; Amendment-02 outputs match their recorded hashes; every hard cell must total exactly 10,000 replicates before classification. The calibration test file is run and its result is required for a qualifying verdict. | No real data |
| `qualify_information_decay_amendment04.py` (synthetic) | Methodology, Amendments 02–04 and the Amendment-04 inventory unchanged since their commits; derived exact-size inventory equals the committed one; ceiling equals the frozen ceiling; the calibration test file passes; each cell covers replicates 0–9,999 exactly | No real data |
| `run_information_decay_primary_v1.py` | Calibration-freeze tag matches its config and is contained in HEAD; frozen library byte-identical to that tag; no modified tracked files and no untracked scientific files; methodology and Amendments 02–04 unchanged; event-response dataset content hash equals its recorded value; each cohort has exactly the frozen count, unique release IDs, a single family, no horizon-specific drift and no missing surprise | It **computes and records** each cohort's release-ID hash but does **not** compare it to a previously frozen expected hash; it does **not** verify any previously frozen output files |
| `run_information_decay_residual_spy_v1.py` | Calibration and primary tags match their config and are in HEAD; frozen library unchanged; primary metadata unchanged since the primary tag and primary output files match their recorded hashes; clean tracked tree; protocol documents unchanged; dataset content hash; residual cohort count and primary-context count equal the frozen counts; release IDs unique; the baseline replica reproduces the stored v2 expected and residual values, residual cohort and residual ranks; the replica equals the frozen `add_baseline` on 25 permuted real frames per family | Records the residual cohort release-ID hash but does not compare it to an earlier frozen value (none existed) |
| `run_information_decay_qqq_replication_v1.py` | Calibration, primary and residual tags match their config and are in HEAD; frozen library unchanged; primary and residual metadata unchanged since their tags and their output files match recorded hashes; clean tracked tree; protocol documents unchanged; dataset content hash; QQQ cohorts have exactly the frozen counts; the rebuilt SPY cohort release-ID hashes equal the frozen SPY primary hashes | Records the QQQ cohort release-ID hashes but does not compare them to an earlier frozen value (none existed) |
| `run_information_decay_ksg_sensitivity_v1.py` | Calibration, primary, residual and QQQ tags match their config and are in HEAD; frozen library unchanged; all three frozen result sets have unchanged metadata and intact output files; clean tracked tree; protocol documents unchanged; dataset content hash; KSG configuration equals the frozen settings (k = 3, 5, 10, tie policy K-A, jitter, bits); every SPY and QQQ cohort release-ID hash equals the frozen metadata | — |

**B. Checks performed afterwards (not runner gates).**

- **Analyst reruns.** The primary, residual, QQQ and KSG runners were each
  rerun after their first run, and every output file was byte-identical.
- **Independent reviews.**
  - The primary SPY, residual SPY, QQQ replication and KSG sensitivity
    results were each independently reviewed and accepted. The KSG review
    reproduced all 90 numerical cells, every permutation p-value and all
    nine output files byte for byte.
  - The review of this report verified 333 numerical comparisons, all 17
    scientific claims, every frozen milestone identity, every recorded
    scientific fingerprint and every output hash.
- **Outcome.** The accepted cohort identities and output hashes were
  independently verified.

---

## 14. Scientific claim audit

| Claim | Supporting evidence | Limitations | Status |
|---|---|---|---|
| CPI m/m surprise–SPY response dependence is detectable at all five horizons | Primary GCMI; horizon-adjusted p = 0.0001 at every horizon; primary Holm-adjusted p = 0.0003 | Retrospective; forecast vintages not point-in-time verified; monotonic-dependence estimator | **SUPPORTED** |
| Core CPI m/m surprise–SPY response dependence is detectable at all five horizons | As above (Holm-adjusted p = 0.0003) | Shares releases and responses with CPI m/m (dependent panel) | **SUPPORTED** |
| No horizon-adjusted NFP dependence detected in SPY (GCMI) | Panel min horizon-adjusted p = 0.1401; Holm not rejected | Absence of detection is not evidence of no effect | **SUPPORTED** (as non-detection only) |
| CPI-family dependence is also detected in QQQ | QQQ GCMI; replication Holm-adjusted p = 0.0003; all horizons | Not independent of SPY; smaller cohort (80) | **SUPPORTED** |
| No horizon-adjusted NFP dependence detected in QQQ | Replication Holm p = 0.383 | Non-detection only | **SUPPORTED** (as non-detection only) |
| Detectable residual CPI-family association remains at short horizons | Null-B residual GCMI; secondary Holm; CPI 1/5/15 min, Core CPI 1/5 min | Baseline-specific; not conditional MI; n = 77 | **SUPPORTED** |
| Residualization removes a substantial portion of the CPI-family association | Matched residual/raw ratios: 35.4374% and 27.8406% at 1 min; 4.87–18.53% at 5–60 min | No delta test; MI not additive; not an explained-information share | **DESCRIPTIVE ONLY** |
| CPI-family detection is robust to the estimator | KSG detects in 12/12 panel/k combinations; 13 + 6 of 20 CPI-family cells detected by KSG for some or all k | KSG sensitivity-only; D8 imbalance; no KSG CI | **SUPPORTED** (panel-level, qualitative) |
| GCMI CPI-family information is highest at 1 minute | Point estimates | Overlapping intervals; no shape test; KSG CPI m/m peaks at 30 min | **DESCRIPTIVE ONLY** |
| The temporal information profile is estimator-independent | — | Contradicted descriptively by KSG | **NOT ESTABLISHED** |
| Statistical information decay | — | No decay test or model pre-specified; profile shape differs by estimator | **NOT ESTABLISHED** |
| Apparent SPY–QQQ differences are largely associated with cohort composition | Full-cohort gaps of +0.05 to +0.11 bits shrink to −0.02 to +0.08 bits on matched releases | No cross-asset test; overlapping intervals | **DESCRIPTIVE ONLY** |
| QQQ is statistically stronger than SPY | — | No difference test was pre-specified | **NOT ESTABLISHED** |
| Residual GCMI measures conditional MI or information unknown to the market | — | The baseline conditions on sign(S); not a conditioning set | **NOT ESTABLISHED** |
| NFP has no market effect | — | Only non-detection under the frozen procedures; one KSG pointwise exception | **NOT ESTABLISHED** |
| Surprises cause the observed responses | — | No causal identification | **NOT ESTABLISHED** |
| Any decision or value-of-information benefit | — | Not studied (M4 proposal) | **NOT ESTABLISHED** |
