# M3 — Information Decay Experiment v1: Methodology (pre-analysis protocol)

| Field | Value |
|---|---|
| Experiment ID | `m3_information_decay_v1` |
| Status | **FROZEN pre-analysis protocol** (commit `Freeze M3 information-decay methodology`). D0 is ratified. D1–D8 stay conditionally open until the synthetic calibration (§9) is complete and recorded in amendment 01 (§21). |
| Branch | `feat/information-decay-analysis` |
| Input dataset (read-only, frozen) | `data/processed/event_response/event_response_v2.parquet`, provenance `metadata/research/event_response_v2.json` (content SHA-256 `a6f83a05…9986`), spec `config/event_research.yaml` (spec v2, surprise spec v2, response features v2, baseline `expanding_family_sign_mean_v1`, split v1) |
| Real-data MI computed so far | **None.** This document was written without computing any surprise–response statistic. Sample-size counts in §7 use only inclusion flags, never S–R pairings. |

This document is a methodology protocol only. It does not implement anything,
and it does not change the M2 market-data layer or the macro-response layer.
Every rule here is fixed before any real-data mutual information (MI) is
computed. A change after that point must be a dated, versioned amendment
(§21) that states whether real MI results had been seen when it was made.

---

## 1. Research question

> How much information does a macroeconomic surprise carry about the subsequent
> market response, and how does that information profile evolve over the
> horizon after the release?

The object of study is an **information profile over horizon**: a curve
`h ↦ I(h)` evaluated at fixed post-release horizons. The project calls this
"information decay", but v1 does **not** assume decay (§3.3). "Decay" can be
used in the results only for a stretch of the profile where a decline after a
peak is empirically supported. A formal decay model needs a later amendment.

All inference in v1 is **retrospective historical inference** about
statistical dependence (§16). It is not out-of-sample prediction and not a
causal estimate.

---

## 2. Formal variables

All variables come from the frozen v2 layer. M3 code reads them and never
recomputes them.

| Symbol | Definition | Frozen column |
|---|---|---|
| `S` | Standardized macro surprise: `(surprise_raw_t − mean_{s<t}) / std_{s<t}`, per family, strictly earlier usable releases only, ≥ 12 history, ddof = 1 | `surprise_std` (with `surprise_std_status == "ok"`) |
| `R_h` | Simple return from the pre-release reference close (bar starting t−1m) to the close of the bar starting t+(h−1)m | `post{h}m_ret` |
| `Rhat_h` | Walk-forward / online expanding mean of the same symbol × family × **surprise-sign** group's strictly earlier usable `R_h` (min history 8). The sign is `surprise_sign = sign(surprise_raw)`, the sign of the *raw* surprise (`actual − forecast`), not of the standardized `S`. The two can differ when the historical mean surprise is non-zero. | `expected_post{h}m_ret` |
| `E_h` | Residual response `R_h − Rhat_h` | `resid_post{h}m_ret` |
| `release_id` | Statistical shock unit | `release_id` |

Horizon sets:

- **Profile horizons (residual and raw):** `H = {1, 5, 15, 30, 60}` minutes.
  This is also written `H_E`.
- **Exploratory raw-only horizon:** 2 minutes.

> **D0: RATIFIED by the project owner.** The frozen M2 expected-response
> baseline (`config/event_research.yaml`, `baseline.horizons`) is defined at
> 1, 5, 15, 30 and 60 minutes only. The v2 dataset has no
> `expected_post2m_ret` or `resid_post2m_ret`. M2 is **not** reopened to
> manufacture a symmetric horizon set. Therefore:
>
> 1. **Primary residual profile:** `I(S; E_h)` for `h ∈ {1, 5, 15, 30, 60}`.
> 2. **Matched raw comparator:** `I(S; R_h)` on **exactly the same releases**
>    (the residual cohort `cs_resid`) and **the same five horizons**. This
>    is the primary raw-versus-residual comparison.
> 3. **Secondary raw profile:** `I(S; R_h)`, `h ∈ {1, 5, 15, 30, 60}`, on
>    the raw common-support cohort `cs_raw`.
> 4. **Exploratory raw-only 2m point:** `I(S; R_2)`, reported separately
>    and labelled `exploratory_raw_only_2m`.
>    - It is **not** part of any primary or secondary horizon profile.
>    - It is **not** part of the matched raw-versus-residual comparison.
>    - It is **never** used to infer anything about residual information
>      decay. There is no residual at 2m to compare it with.
>    - It sits outside every multiplicity family and every max-statistic
>      horizon set.

**Terminology rule.** `I(S; E_h)` is MI between the surprise and a *residual
response*. It must never be called "conditional mutual information" or
written `I(S; R_h | pre-event information)`. `Rhat_h` is a historical
group-mean benchmark, not a conditioning set. Genuine conditional MI is
reserved for M4 (§19.3).

**Units.** Every MI quantity is reported in **bits**. Estimators that return
nats are converted with `bits = nats / ln 2` at the estimator boundary, and
nats never appear in stored outputs.

### 2.1 THE RESIDUAL-NULL PROBLEM (critical; part of the protocol, not a footnote)

`Rhat_h` is conditioned on the sign of the surprise, so `E_h` is **not** a
transformation of the response built independently of the surprise:

`E_h = R_h − m_{sign(surprise_raw), t}`,

where `m_{g,t}` is the expanding mean of earlier responses in sign group `g`.
The residual depends on the surprise through the sign group it was assigned
to. Three consequences are binding.

1. **A mechanical source of dependence.** `S ⟂ R_h` does **not** imply
   `S ⟂ E_h`. Under a true data-generating null with `S ⟂ R`, the estimated
   residual can still show finite-sample dependence on `S`. Each sign group's
   expanding mean carries its own estimation noise, a group-specific offset
   that persists over time and is shared by every release in the group. That
   offset is a function of `sign(S)`.
2. **The naive permutation null may be wrong for residuals.** "Fixed-residual"
   permutation holds `E` fixed and permutes `S`. It destroys this mechanical
   dependence in the permuted samples but keeps it in the observed sample, so
   it can inflate type-I error. The protocol therefore defines **two**
   residual nulls, fixed-residual (Null A) and pipeline-aware (Null B), in
   §11.7. Synthetic calibration (§9, scenario 8) decides between them under
   rule D4 (§10.3). Residual MI is the *intended* primary quantity, and its
   primary status is **conditional on D4**.
3. **Sign removal absorbs much of a linear signal.** This is an analytic fact,
   not a data result. Take a linear Gaussian relation `R = ρS + noise` with
   `S ~ N(0, 1)`, and suppose the baseline estimates the sign-group means
   perfectly. Subtracting `E[R | sign S] = ρ·sign(S)·E|S|` leaves a residual
   whose correlation with `S` is only `ρ(1 − 2/π) / √(1 − 2ρ²/π)`, about
**0.36ρ / √(1 − 0.64ρ²)**. For
   ρ = 0.35 that is about 0.13. The residual profile is therefore mainly a
   measure of dependence *beyond* the historical sign-average response, such
   as magnitude or deviation from the sign mean. It can be smaller **or
   larger** than `I(S; R_h)`, and neither ordering is a finding by itself.
   This is why the matched raw comparator on identical releases is mandatory.

---

## 3. Hypotheses

### 3.1 Primary (confirmatory) hypothesis family

For each primary panel `j ∈ {SPY × CPI_MOM, SPY × CORE_CPI_MOM, SPY × NFP}`:

- `H0_j`: within each chronological stratum (§11.2), the surprise values are
  exchangeable across releases with respect to the response process from
  which the residual vector `(E_1, E_5, E_15, E_30, E_60)` is built. The
  exact null construction, fixed-residual or pipeline-aware, is set by D4
  (§2.1, §11.7).
- `H1_j`: at least one horizon shows detectable statistical dependence.

**Primary estimand:** the effective-MI residual profile
`MI_eff^{E}(h) = MI_obs(S; E_h) − mean_perm MI(S; E_h)`, `h ∈ H_E`, on the
residual common-support cohort, using the primary estimator (§10).

The family has three hypotheses, deliberately small. Multiplicity control is
in §13.

### 3.2 Secondary and replication analyses (not in the discovery family)

| Label | Content |
|---|---|
| Matched-raw (primary comparator) | `I(S; R_h)`, `h ∈ {1, 5, 15, 30, 60}`, on `cs_resid`: the same releases and same horizons as the primary residual profile |
| Secondary-raw | `MI_raw(h) = I(S; R_h)`, `h ∈ {1, 5, 15, 30, 60}`, SPY, primary families, raw common-support cohort `cs_raw` |
| Exploratory raw-only 2m | `I(S; R_2)` on `cs_raw`, a single point outside every profile and multiplicity family (§2) |
| Replication-QQQ | All of the above for QQQ, analysed separately from SPY |
| Exploratory tier | Families listed in §4.2 |

Secondary and replication results get horizon-adjusted p-values (§13.1) but
sit outside the primary Holm family. They are labelled
`secondary` / `replication` / `exploratory` in every output.

### 3.3 No monotonic-decay assumption

The protocol imposes no shape constraint. The profile may rise, peak, plateau,
decline or rebound, for example around the session boundary (§15). Nothing is
fitted. In particular, no `I(h) = I0·exp(−λh)` is fitted in v1.

Exploratory shape summaries are computed for description only. They carry no
hypothesis test:

- `I_peak = max_h MI_eff(h)`, and `h_peak = argmax_h MI_eff(h)` with ties
  going to the shortest horizon;
- retention `ρ(h) = MI_eff(h) / I_peak` for `h > h_peak`, reported only if
  `I_peak > 0`;
- `h_{<50%}` = first observed horizon after `h_peak` with `ρ(h) < 0.5`, or
  "none".

`h_{<50%}` is **not** called a half-life. That word needs a pre-specified
model of monotonic post-peak decline (future amendment). Each summary is
reported with its bootstrap distribution (§12). The bootstrap distribution of
`h_peak` is reported as frequencies, never as a confidence interval.

---

## 4. Event-family scope

### 4.1 Primary families

| Family | Reason |
|---|---|
| `CPI_MOM` | ~127 releases 2016–2026; economically interpretable surprise; existing response evidence |
| `CORE_CPI_MOM` | Same |
| `NFP` | Same; the surprise is nearly continuous |

Families are not added to increase the result count.

`CPI_MOM` and `CORE_CPI_MOM` are released together. In the SPY cohorts all
93 raw-cohort releases are shared, so the two panels use **identical response
vectors** and differ only in which surprise component is used. They are two
correlated questions about one set of market shocks, not two independent
samples. This dependence is why Holm is chosen over BH (§13.2).

### 4.2 Exploratory tier (outside every confirmatory family)

`UNEMPLOYMENT_RATE`, `AVG_HOURLY_EARNINGS_MOM`, `CORE_PCE_MOM`,
`GDP_ADVANCE_QOQ`, `GDP_PRELIM_QOQ`, `GDP_FINAL_QOQ`.

- Unemployment and AHE share every release with NFP, so they share NFP's
  responses. Any cross-family view must keep release components linked (§6).
- `CORE_PCE_MOM` mixes 08:30 and 10:00 ET releases (SPY raw cohort: 70 at
  08:30, 9 at 10:00). For a 10:00 release every horizon falls in the regular
  session, so the horizon has a different market-session meaning. Exploratory
  Core PCE profiles therefore use **08:30 releases only**. Pooling the two
  release times is not allowed.
- GDP families have residual common-support n between 3 and 9. Below any
  plausible threshold they are reported as counts only, with no MI.

### 4.3 FOMC excluded

`FED_FUNDS_RATE` has `surprise_measure_quality = weak_surprise_proxy`, with
only 2 non-zero rate surprises in 2016–2026. v1 computes **no** MI for FOMC,
primary or exploratory. FOMC response rows stay available in the frozen layer
for later work that has a market-implied surprise (fed funds futures or OIS).

---

## 5. Market scope

- **Primary market: SPY.** Early-history pre-market coverage is materially
  better for SPY than for QQQ (§7).
- **Replication market: QQQ.** It provides cross-asset replication and
  heterogeneity evidence only.
- **No pooling.** SPY and QQQ observations are never pooled into one sample.
  They react to the same release shocks and are strongly dependent, so QQQ
  does not double n.
- When SPY and QQQ are analysed jointly, for example as a cross-symbol
  contrast or for joint multiplicity calibration in a later amendment, the
  analysis uses the **intersection cohort**: releases in both symbols' cohorts.
  One release permutation is applied to both symbols (§11.4).

---

## 6. Observation unit

The statistical unit is **`release_id`**, not a dataframe row.

- The frozen guard `filters.require_single_family` applies. A family-specific
  panel has at most one row per `release_id` per symbol, which has been
  verified for every family and symbol in v2.
- Permutation (§11) and bootstrap (§12) resample **release IDs**. A resampled
  release brings its complete horizon vector `(R_h)` or `(E_h)` and its
  surprise.
- Any analysis that combines families works on `filters.release_level`
  output. All surprise components of one release stay linked: they are
  permuted together and bootstrapped together. Components of one simultaneous
  release (CPI/Core CPI; NFP/Unemployment/AHE) must never become
  pseudo-independent observations.

---

## 7. Common-support cohort

### 7.1 Primary rule: common horizon support

Mixing sample compositions across horizons would confound the horizon effect
with a sample-composition effect. Every point on one primary information
curve therefore uses **exactly the same set of releases**.

A cohort is defined per `family × symbol × response type`. A release enters
it only if **all** of the following hold:

1. `event_status == "usable"`. This excludes ambiguous, conflicting,
   non-standard and unscheduled timestamps, and anything after the research
   end.
2. `surprise_std_status == "ok"` and `surprise_std` is non-null
   (`filters.usable_std_surprise`).
3. `provisional_market_data` is false.
4. For **every** horizon in the profile's horizon set, the window status is in
   `{ok, ok_market_halt}` and `post{h}m_ret` is non-null
   (`filters.usable_response`). This automatically excludes
   `provider_gap_in_window`, `exchange_closed`, `insufficient_market_window`,
   `provisional_market_data` and `event_excluded` windows.
5. For a **residual** cohort, additionally `baseline_post{h}m_status == "ok"`
   and `resid_post{h}m_ret` is non-null for every `h ∈ H_E`.

| Cohort ID | Horizon set | Used by |
|---|---|---|
| `cs_resid` | `{1, 5, 15, 30, 60}` (rules 1–5) | **Primary** residual profile and the matched-raw comparator |
| `cs_raw` | `{1, 5, 15, 30, 60}` (rules 1–4) | Secondary-raw; also the exploratory 2m point, on releases of `cs_raw` with a usable 2m window. Currently that is all of them. |
| `ac_raw(h)`, `ac_resid(h)` | single horizon | Available-case sensitivity only (§7.3) |
| `cs_resid_preopen`, `cs_raw_preopen` | `{1, 5, 15, 30}` | Market-open sensitivity only (§15) |

The code that builds cohorts must reuse `src/research/filters.py` helpers. It
must not re-implement inclusion logic.

### 7.2 Anticipated sample sizes (read-only inventory of frozen v2)

These counts come only from the inclusion flags above. No surprise–response
statistic was computed.

**Common support**

| Family | Symbol | Rows | Usable | Valid S | `cs_raw` (5h) | `cs_resid` (5h) | `cs_raw_preopen` | `cs_resid_preopen` |
|---|---|---|---|---|---|---|---|---|
| CPI_MOM | SPY | 127 | 127 | 115 | 93 | **77** | 99 | 83 |
| CORE_CPI_MOM | SPY | 127 | 127 | 115 | 93 | **77** | 99 | 83 |
| NFP | SPY | 129 | 128 | 116 | 103 | **97** | 108 | 103 |
| CPI_MOM | QQQ | 127 | 127 | 115 | 80 | 56 | 89 | 65 |
| CORE_CPI_MOM | QQQ | 127 | 127 | 115 | 80 | 56 | 89 | 65 |
| NFP | QQQ | 129 | 128 | 116 | 89 | 74 | 100 | 87 |

The matched-raw comparator uses `cs_resid` itself: the same releases, raw
returns at the same five horizons. `cs_raw` on five horizons has the same
counts as on six, because every `cs_raw` release also has a usable 2m
window.

**Available case by horizon (valid S and usable response)**

| Family | Sym | raw 1 | raw 2 | raw 5 | raw 15 | raw 30 | raw 60 | res 1 | res 5 | res 15 | res 30 | res 60 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CPI_MOM | SPY | 111 | 111 | 109 | 105 | 99 | 93 | 99 | 97 | 91 | 83 | 77 |
| NFP | SPY | 112 | 112 | 112 | 111 | 108 | 103 | 107 | 107 | 106 | 103 | 97 |
| CPI_MOM | QQQ | 106 | 106 | 102 | 95 | 89 | 80 | 85 | 80 | 72 | 65 | 56 |
| NFP | QQQ | 108 | 107 | 107 | 103 | 100 | 89 | 99 | 98 | 92 | 87 | 74 |

Core CPI's available-case counts equal CPI's.

**Year and stratum composition of primary cohorts**

| Cohort | Family × Sym | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | dev / val / test |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `cs_resid` | CPI SPY | 0 | 1 | 4 | 6 | 11 | 12 | 12 | 12 | 10 | 9 | 11 / 23 / 43 |
| `cs_resid` | Core CPI SPY | 0 | 0 | 3 | 8 | 11 | 12 | 12 | 12 | 10 | 9 | 11 / 23 / 43 |
| `cs_resid` | NFP SPY | 3 | 10 | 11 | 11 | 10 | 12 | 11 | 11 | 10 | 8 | 35 / 22 / 40 |
| `cs_resid` | CPI QQQ | 0 | 0 | 0 | 0 | 4 | 10 | 11 | 12 | 10 | 9 | 0 / 14 / 42 |
| `cs_raw` | CPI SPY | 3 | 10 | 4 | 10 | 11 | 12 | 12 | 12 | 10 | 9 | 27 / 23 / 43 |
| `cs_raw` | NFP SPY | 8 | 10 | 12 | 11 | 10 | 12 | 11 | 11 | 10 | 8 | 41 / 22 / 40 |

No 2016 release qualifies, because the 12-release surprise-standardization
history ends in early 2017. All primary releases are at **08:30 ET**.

**Why releases drop out (SPY CPI, 34 dropped from the 127 usable).**

- 12 have insufficient surprise history.
- 24 have an `insufficient_market_window` at 60m: at least one pre-market
  minute had no trade, mostly in early years.
- 2 fall on an exchange-closed day.

`cs_raw → cs_resid` drops a further 16 CPI and 6 NFP releases because a
sign group had fewer than 8 baseline history releases at 60m. The baseline
needs ≥ 8 earlier same-sign releases, so the residual cohort loses each sign
group's earliest releases. It also loses the single zero-surprise NFP release:
the zero sign group never reaches 8.

**Consequences, fixed now:**

- All three SPY primary residual cohorts are at or above the provisional
  primary threshold of 60 (§10.4).
- QQQ CPI and Core CPI residual cohorts (n = 56) are **below 60**. Unless the
  calibrated threshold (§10.4) is ≤ 56, they are **exploratory replication**.
  The common-support rule is **not** relaxed to raise QQQ n.
- The residual cohorts lean toward 2021–2026. In the QQQ CPI residual cohort
  the development stratum is empty. The stratum-merging rule (§11.2) handles
  this mechanically.

### 7.3 Available-case sensitivity

The **available-case information profile** computes MI at each horizon on
every release valid *at that horizon*. Its n varies by horizon and is
reported at every point. It is a sensitivity analysis, not the primary decay
curve. Its only purpose is to show whether common-support filtering
materially changes the profile. It never stands in for the primary curve
when a primary result is disappointing.

### 7.4 Required pre-MI diagnostic

The implementation must write the count tables of §7.2 (regenerated, not
copied) before any MI computation runs. It must also write one selection
diagnostic: the distribution of `|S|` for releases inside versus outside each
primary cohort. It must not write anything that pairs `S` with a response.

---

## 8. Estimator candidates

### 8.1 A. Gaussian-copula MI (GCMI). Candidate primary.

Ince et al. (2017). Each marginal is transformed to standard normal scores
through its empirical CDF (ranks), and MI is computed in closed form for the
resulting Gaussian variables. For two scalar variables this reduces to

`I_GC = −½ · log2(1 − r_ns²)`,

where `r_ns` is the Pearson correlation of the normal scores.

Why it is attractive:

- It is rank-based, so it is invariant to monotone marginal transforms and
  robust to heavy tails and outliers in returns.
- It is deterministic, cheap and numerically stable at n ≈ 50–120.
- Permutation inference is trivial. With common support and no ties, the
  copula-normalized response has the same marginal at every horizon, so the
  horizon-wise permutation nulls are identically distributed. That makes the
  max-statistic (§13.1) balanced across horizons.

Assumptions and limitations, stated up front:

- **It is a lower bound under a Gaussian-copula assumption.** It equals the
  true MI only when the dependence structure is a Gaussian copula. For any
  other copula, the value is the MI of the Gaussian copula with the same
  normal-score correlation, which by the max-entropy property of the Gaussian
  bounds the true MI from below.
- **For scalar–scalar pairs it is essentially a monotonic-association
  measure.** It is close to zero for non-monotonic dependence such as a
  symmetric U-shape, and it misses heteroskedastic dependence, where `|S|`
  scales the spread of `R`. Both are economically plausible: a large surprise
  of either sign can move the market more. A GCMI profile must therefore be
  labelled as **monotonic (Gaussian-copula) information**, never as "total
  information".
- **Ties.** CPI and Core CPI surprises are strongly discretised. In the SPY
  raw cohort, `surprise_raw` takes 9 distinct values, with 33–38% exactly
  zero. `surprise_std` breaks those ties only through the time-varying
  standardization mean and std, so its within-tie ordering is effectively a
  function of time. This must be calibrated (§9, scenario 9; §21, D3).

The closed-form Gaussian-entropy bias correction from Ince et al. is computed
and stored as a diagnostic. The **primary bias correction is the permutation
null (§11.5)**, so the two corrections are never stacked.

### 8.2 B. Kraskov–Stögbauer–Grassberger (KSG). Robustness estimator.

Kraskov, Stögbauer & Grassberger (2004), algorithm 1, max-norm, at fixed
`k ∈ {3, 5, 10}`. Each variable is z-scored before estimation, and seeded
noise (sd 1e-10 in z-units, §17) is added only to break exact ties.

Why it is attractive:

- It is a nonparametric k-nearest-neighbour estimator with no binning and no
  copula assumption.
- It detects non-monotonic and heteroskedastic dependence that GCMI misses.
- It has low bias for weak dependence relative to bin estimators.

Tradeoffs:

- `k` trades bias against variance. A small `k` lowers bias and raises
  variance; a large `k` does the reverse. At n ≈ 60–100 the variance is
  substantial.
- Its finite-sample null bias is not zero. It can go negative under
  independence, which is one reason MI is reported as permutation-corrected.
- It assumes continuous variables. Discrete or near-discrete `S` (CPI) and
  duplicated observations (bootstrap) break the nearest-neighbour counting
  logic. Mixture-aware variants such as Gao et al. (2017) and Ross (2014)
  exist, but are not in v1 unless calibration requires an amendment.

**k is fixed at {3, 5, 10} for every panel.** It is never tuned per family,
horizon or symbol. All three values are reported.

### 8.3 C. Gaussian / linear benchmark

`I_lin = −½ · log2(1 − ρ_P²)` with Pearson `ρ_P`. It is a transparent sanity
check only, never a primary nonlinear MI estimator.

### 8.4 D. Histogram / binned MI

Excluded from all inferential outputs. Small-n bin MI depends strongly on the
binning. It may appear only in the calibration report as a pedagogical
comparison: equal-frequency bins, with `b = 4` fixed in advance.

### 8.5 Correlation benchmarks

Every MI panel also reports Pearson and Spearman correlation at each horizon,
with the same cohort, the same permutations (giving two-sided permutation
p-values) and the same bootstrap. They separate dependence that is mainly
linear or monotonic, where GCMI ≈ `I_lin` and Spearman carries it, from
dependence that MI captures beyond correlation, where KSG exceeds GCMI by more
than the null spread. Correlation never replaces MI.

### 8.6 Why estimator agreement and disagreement are informative

The estimators make different assumptions, so their pattern of agreement is
itself evidence:

| Pattern | Reading |
|---|---|
| GCMI ≈ KSG ≈ `I_lin`, Spearman ≈ Pearson | Dependence is essentially linear/monotonic; the GCMI lower bound is close to tight |
| KSG ≫ GCMI (beyond null spread), Spearman small | Non-monotonic or heteroskedastic dependence that GCMI cannot see |
| GCMI significant, KSG not | Monotonic signal too weak for the higher-variance KSG at this n; not a contradiction |
| `I_lin` ≫ GCMI | Linear correlation driven by a few extreme observations, which ranks down-weight; check the influence sensitivities (§14, items 6–7) |
| KSG varies strongly with k | KSG is unstable at this n; KSG evidence is weak |

These readings are descriptive guides. They are not additional tests.

---

## 9. Synthetic calibration plan (runs before any real MI)

The calibration is a separate deliverable: the next implementation task after
this freeze. **No real-data MI is computed until the calibration report and
the amendment (§21) recording its decisions are committed.**

### 9.1 Design

**Sample sizes:** the grid `n ∈ {40, 60, 80, 100, 120}`. The real cohort
sizes already documented in §7.2, **{56, 74, 77, 97}**, are added as
eligibility points for D2 (§10.4). They are fixed now because 77 and 97 lie
between grid points.

**Replications (minimums fixed now):**

- **GCMI cells:** 2,000 Monte Carlo replicates per scenario × n cell, for
  bias, SD, RMSE, type-I error and power. The MC SE of a rejection rate near
  0.05 is about 0.005. Inside each replicate: B = 1,000 stratified
  permutations and 500 bootstrap replicates.
- **KSG permutation cells:** at least 1,000 Monte Carlo replicates, with at
  least 200 permutations per replicate. KSG cost is material. The final
  counts are the largest a pre-run *runtime benchmark* (timing only, with no
  estimator outputs inspected) shows to be feasible within about 2 hours of
  wall time. They are written into `config/information_decay_v1.yaml`
  before the final run.
- **Bootstrap and subsampling coverage cells:** at least 1,000 replicates for
  GCMI and at least 500 for KSG.
- **Development runs:** may use smaller counts and are labelled `dev`. Only
  the `final` run feeds amendment 01.

These are reduced in-replicate counts so the simulation is computable. The
final real-data counts (§11.6, §12) are higher and are not tuned.

**Horizon structure:** each replicate generates a **5-column** response matrix
for `h ∈ {1, 5, 15, 30, 60}`. The noise is cumulative: `Z_h = W(h)/√h` for a
standard Brownian motion `W`, which is a common latent shock plus independent
increments. So `corr(Z_h, Z_h') = √(min(h, h') / max(h, h'))`: 0.45 for
1–5m and 0.71 for 30–60m. This structure is fixed a priori from the
cumulative-return construction and is not fitted to real data. Signal terms
are added per scenario.

**Strata:** releases in time order are assigned to three strata in fixed
proportions 0.30 / 0.25 / 0.45, with the merge rule of §11.2.

**Surprise-construction modes (D6):**

- **Mode A, direct:** `S ~ N(0, 1)` iid.
- **Mode B, pipeline-faithful:** a raw surprise is generated and passed
  through the frozen causal expanding standardization: strictly earlier
  values only, at least 12 of them, ddof = 1. The first 12 releases are
  burn-in. They carry a raw sign but no `S`, exactly as in v2.
  - Continuous raw (NFP-like): `N(0.3, 1)`.
  - Discrete raw (CPI-like): the grid below.

**CPI-like discrete raw surprise:** this is a fixed distribution based only on
the marginal facts already recorded in this document before any MI (9
distinct values, ~35% zero):

| Value | P |
|---|---|
| 0 | 0.35 |
| −0.1 | 0.22 |
| +0.1 | 0.20 |
| −0.2 | 0.08 |
| +0.2 | 0.07 |
| ±0.3 | 0.03 each |
| ±0.4 | 0.01 each |

**Real data:** the calibration reads **no** real surprise, response or
residual values. The plasmode option of the draft is **dropped**: every
distribution is synthetic. A code-level guard blocks reads of
`data/processed`, `data/interim`, `data/raw` and every `.parquet` file while
the calibration runs.

### 9.2 Scenarios

Numbering follows the M3 calibration brief. Unless stated, a non-Gaussian
scenario's strength parameter is set numerically so that its true per-horizon
MI equals the reference effect `I_ref = −½·log2(1 − 0.35²) ≈ 0.0976` bits.
Its null version (strength 0) is also run. True MI comes from high-precision
quadrature, and the calibrated parameters are written to the outputs.

| # | Scenario | Generative model (each horizon; `Z_h` cumulative noise) | Truth |
|---|---|---|---|
| 1 | Independence | `R_h = Z_h`, `S` independent; mode A and mode B (continuous) | 0 |
| 1h | Independence, heterogeneous horizon margins (for D8) | `R_1 ∝ t_3`-mixed `Z_1`, `R_5 ∝ t_5`-mixed `Z_5`, other horizons Gaussian | 0 |
| 2 | Linear Gaussian | `R_h = ρS + √(1−ρ²)·Z_h`, `ρ ∈ {0.15, 0.25, 0.35, 0.50}`; mode A, plus mode B at ρ = 0.35 | `−½·log2(1−ρ²)` |
| 3 | Monotone nonlinear | (a) Gaussian copula ρ = 0.35 with non-Gaussian margins, `S' = S³` and `R'_h = exp(1.5·R_h)` (MI is invariant, so the truth is analytic); (b) `R_h = tanh(βS) + σZ_h` at `I_ref` | (a) analytic; (b) quadrature |
| 4 | U-shaped | `R_h = β(S² − 1) + Z_h` at `I_ref` (Pearson ≈ 0) | Quadrature |
| 5 | Heteroskedastic | `R_h = (1 + β|S|)·Z_h` at `I_ref` | Quadrature |
| 6 | Heavy-tailed noise | `R_h = βS + Z_h/√(χ²_3/3)` (multivariate t_3, shared mixing); null and at `I_ref` | Quadrature |
| 7 | Outlier contamination | Scenario 2 at ρ = 0 and ρ = 0.35. A fixed fraction `max(1, round(0.02n))` of releases is replaced by `S = 4u`, `R_h = −10u` with `u = ±1` (against the dependence). The clean version of the same replicate is kept for paired shifts. | Clean MI |
| 8 | **Sign-conditioned baseline artefact** | `S ⟂ R` (scenario 1 and 6 nulls; mode A, mode B continuous and mode B discrete). `E_h` is built with the frozen M2 rule (§9.4). Fixed-residual Null A and pipeline-aware Null B are compared. Also an informational alternative: raw ρ = 0.35, then residualized. | 0 (for `I(S; R)`) |
| 9 | **Discrete / tied surprise** | Mode B discrete. The response has a slow time trend: mean `0.6·(t/N − ½)`, sd `1 + t/N`. `S_raw ⟂ R` (null), plus alternative `R_h = 0.35·z_raw + …`. The D3 tie policies (§10.3) are compared. | 0 under the null |
| 10 | Regime null | `S ⟂ R` within strata. Stratum means of `S` and of `R` are `(−0.4, 0, 0.4)` and stratum SDs `(1, 1.5, 0.8)`. Stratified and unrestricted permutation are compared. | 0, conditional on stratum |

Scenario 8 decides D4. Scenario 9 decides D3. Modes A and B decide D6.
Scenario 10 justifies stratified permutation: unrestricted permutation is
expected to reject there, and stratified permutation must not.

### 9.3 Metrics

Each scenario is evaluated with:

- GCMI (each D3 tie policy where relevant);
- KSG with k = 3, 5, 10;
- `I_lin`, Pearson and Spearman;
- histogram MI (equal-frequency bins, b = 4, calibration-only illustration).

| Metric | Definition |
|---|---|
| Bias, SD, RMSE | Against the true MI, per estimator × n × scenario (and ρ) |
| Null bias | `E[MI_obs]` under the null, which is what `MI_eff` removes. `E[MI_eff]` and `P(MI_eff < 0)` are reported. |
| Type-I | Rejection rate at α = 0.05 under nulls, with a **Wilson 95% interval**. Reported **pointwise** (per horizon, unadjusted) and **horizon-adjusted** (max-T; panel rejects if any adjusted p ≤ 0.05). |
| Power | Same two forms under alternatives |
| Outlier robustness | Median `|MI(contaminated) − MI(clean)|` on paired replicates |
| Stability | SD and coefficient of variation; for KSG, also the spread across k |
| Interval coverage | Coverage of the true MI by each candidate interval of `MI_eff` (§12.2) |

**Type-I acceptance region (fixed now):** an empirical rejection rate `p̂` from
`R` replicates is acceptable if `p̂ ≤ 0.05 + 2·√(0.05·0.95/R)`. That is
0.0597 for R = 2,000 and 0.0638 for R = 1,000. Being below nominal is not a
failure.

### 9.4 Simulation replica of the M2 baseline

Calibration code needs an array implementation of the frozen rule:

- the expanding mean of strictly earlier same-group responses;
- the group is `sign(surprise_raw)`, with groups −1, 0 and +1;
- at least 8 history releases, separately for each horizon.

A unit test must show it reproduces `src/research/baseline.add_baseline` on
synthetic frames. That code is imported read-only and is not modified.
Synthetic releases are equally spaced in time, so there are no same-timestamp
ties.

---

## 10. Decision rules (synthetic evidence only)

The hierarchy for every estimator or procedure choice is fixed:

1. correct type-I behaviour;
2. acceptable bias;
3. acceptable stability and RMSE;
4. robustness to ties, heavy tails and outliers;
5. power.

**The most powerful but miscalibrated option loses.**

### 10.1 Default prior

The prior, which can be overturned:

- GCMI is primary;
- KSG with k ∈ {3, 5, 10} is a robustness family, with no single k chosen;
- `MI_eff` comes from permutation-null subtraction;
- pipeline-aware permutation may be needed for residuals.

### 10.2 Validity criteria (D1)

All of these must hold at every grid n ≥ 60:

- **V1. Size.** In scenarios 1, 6-null and 10, stratified permutation keeps
  the horizon-adjusted type-I rate inside the acceptance region (§9.3).
- **V2. Artefacts.** In the scenario 9 null, the horizon-adjusted type-I rate
  is inside the acceptance region under the D3-selected tie policy.
- **V3. Bias and lower-bound integrity.** In scenarios 2, 3, 6 and 7, the
  mean of `MI_eff` does not exceed the true MI by more than
  `2·MC SE + 0.10·I_true`.
- **V4. Outlier robustness.** In scenario 7, the median paired absolute shift
  is no worse than that of KSG k = 5.

### 10.3 Decision rules D1–D8

**D1, primary estimator.**

- If GCMI satisfies V1–V4, it is primary.
- Otherwise, the passing KSG with the largest k becomes primary.
- If nothing passes, real-data MI is stopped pending revision.
- Power differences in scenarios 4 and 5 do not trigger a switch. GCMI's
  blindness there is known a priori (§8.1). It is handled by the
  "monotonic information" label and by the KSG sensitivity.

**D2, minimum n.** See §10.4.

**D3, tie policy.** It is chosen from scenario 9 among a fixed set.

- GCMI policies:
  - **A:** average ranks of the frozen `surprise_std`;
  - **B:** average ranks within exact `surprise_raw` tie groups, with groups
    ordered by median `surprise_std`;
  - **C:** ranks of `surprise_std` with ties *within* raw groups broken by a
    seeded random order.
- KSG policies:
  - **K-A:** z-scored `surprise_std` with seeded 1e-10 jitter;
  - **K-B:** raw-group-collapsed values (policy-B normal scores) with seeded
    1e-10 jitter.
- **Rule:** keep the policies whose scenario-9 null horizon-adjusted type-I
  rate is in the acceptance region at every grid n ≥ 60. Among those, choose
  in the order A > B > C (K-A > K-B). Skip a policy only if a later one has a
  null bias lower by more than 2 MC SE *and* an SD no larger. Every policy is
  deterministic given its seed.

**D4, residual primacy and residual null.** Decided from scenario 8.

- If **Null A** (fixed residual) inflates type-I, meaning its horizon-adjusted
  rate falls outside the acceptance region at any grid n ≥ 60 in any
  scenario-8 null variant, then v1 residual inference **must** use **Null B**
  (pipeline-aware).
- If both are calibrated, use the simpler Null A and report the evidence.
- If Null B also fails, residual MI loses primary status. Raw MI on `cs_raw`
  becomes the primary quantity under the same family, horizons and
  multiplicity rules, and the residual profile becomes secondary.

**D5, CI method for the primary estimator.** Candidates:

- (a) stratified percentile bootstrap, with duplicate ties broken by seeded
  random order;
- (b) the same, recentred by the bootstrap-mean bias;
- (c) stratified m-out-of-n subsampling without replacement, `m = ⌊0.7·n_s⌋`
  per stratum, with √m/√n rescaling.

Each interval is shifted by the full-sample null mean, so it is an interval
for `MI_eff`. The criterion is coverage of the true MI in scenarios 2
(ρ ∈ {0.15, …, 0.5}) and 3a at grid n ≥ 60.

- A method is **eligible** if its minimum coverage over cells with
  ρ ≥ 0.25 is ≥ 0.90.
- Choose the eligible method with the smallest mean `|coverage − 0.95|` over
  all cells.
- If none is eligible, choose the one with the highest minimum coverage, and
  label every interval "approximate (under-covering in calibration)".
- Width is reported but **never** used to choose.

**D6, synthetic standardization policy.**

- Mode B (pipeline-faithful) becomes canonical if it differs materially from
  mode A in:
  - type-I acceptance (one passes and the other fails);
  - panel power at ρ = 0.35 (|Δ| > 0.05); or
  - `MI_eff` bias (|Δ| > 2 combined MC SE).
- Otherwise mode A is canonical and mode B is reported.
- D2 and D4 always use the stricter of the two modes.

**D7, KSG uncertainty.**

- KSG gets an interval method (bootstrap percentile or subsampling) only if
  that method meets the D5 eligibility bar for **every** k.
- Otherwise KSG gets **no formal CI**. It reports the permutation null SD,
  the subsampling spread and the k-spread as descriptive sensitivity only.

**D8, KSG max-statistic studentization.**

- In scenario 1h, if for any k and grid n ≥ 60 the largest per-horizon null
  mean is more than 1.2 times the smallest, the KSG max-statistic uses
  `(MI − null mean)/null SD`.
- Otherwise the raw MI is used.

### 10.4 Minimum sample threshold (D2)

- **Provisional, until calibration:** `n < 60` is exploratory.
- **Rule (fixed now):** `n_min` is the smallest grid n at which the primary
  estimator and procedure (stratified permutation, max-T, D3 tie policy)
  satisfy **all** of:
  - (i) V1 and V2;
  - (ii) **horizon-adjusted panel power ≥ 0.80** in scenario 2 at
    ρ = 0.35 at all horizons (the probability that max-T rejects at
    α = 0.05);
  - (iii) `|mean MI_eff − I_ref| ≤ 0.20·I_ref`.

  All three are evaluated in **both** D6 modes.
- **Panel eligibility** is checked at the panel's *exact* cohort size, using
  the added points {56, 74, 77, 97}. A primary panel whose exact n fails
  (i)–(iii) is demoted to exploratory, and Holm's m shrinks. If all three SPY
  panels fail, the experiment goes to revision.
- Pointwise power, unadjusted per horizon, is reported but **never** defines
  `n_min`.
- The SD of `MI_eff` at the reference effect is reported as precision
  information.

  > **Pre-freeze correction, made before any data result.** The draft
  > criterion "SD(`MI_eff`) ≤ 50% of `I_ref`" was dropped. Analytically, the
  > CV of an MI estimate is about twice the CV of `r`. The draft criterion
  > therefore needs `r/SE(r) ≳ 4`, roughly n ≈ 120, above every real cohort.
  > It would have demoted every panel by construction and is inconsistent
  > with the 80%-power criterion (`r/SE ≈ 2.8`).
- The threshold is recorded in amendment 01 before real MI and is never
  changed afterwards.
- The **residual-pipeline power** (scenario 8 alternative) is reported for
  information. It shows how much a raw ρ = 0.35 signal survives residualization
  (§2.1), but it is not a D2 criterion.

---

## 11. Permutation inference

### 11.1 Null and mechanics

For each panel, test `S ⟂ response` by permuting **surprise values across
release IDs** within strata. The response *matrix* (all horizons of a
release) stays fixed and intact, and one permutation generates the statistic
at all horizons simultaneously.

### 11.2 Regime-aware stratification (primary)

**Strata**, using America/New_York release dates, inclusive:

- development: 2016-01-01 → 2020-12-31
- validation: 2021-01-01 → 2022-12-31
- test era: 2023-01-01 → 2026-09-30

These boundaries reuse the frozen split dates **only as regime strata** (§16).

**Merging rule (fixed):** a stratum with fewer than 8 releases in a given
cohort is merged into the chronologically next stratum. The last stratum
merges backward. The merged layout is recorded per panel. Examples:

- QQQ CPI residual: development 0 → merged, giving {val+dev, test}.
- SPY CPI residual: 11 / 23 / 43 → no merge.

### 11.3 Unrestricted permutation

Unrestricted permutation across the whole cohort is a **sensitivity** analysis
(§14, item 5), not the primary null.

### 11.4 Shared permutations

- Permutations are generated per **(family, cohort)** and reused across
  horizons (required for the max-statistic), across estimators, and across
  raw versus matched-raw on the same cohort.
- Joint SPY–QQQ analyses use the intersection cohort, with **one**
  permutation applied to both symbols. This preserves their cross-symbol
  dependence.
- In multi-family analyses at release level, the whole surprise-component
  vector of a release moves together.

### 11.5 Statistics stored per (panel, horizon, estimator)

| Field | Content |
|---|---|
| `mi_observed_bits` | Estimate on real pairing |
| `mi_null_mean_bits`, `mi_null_sd_bits` | Over B permutations |
| `mi_effective_bits` | `mi_observed − mi_null_mean`. **Not truncated at 0**: a negative value honestly signals no detectable information plus estimator noise |
| `p_unadjusted` | `(1 + #{null_h ≥ obs_h}) / (B + 1)` |
| `p_horizon_adjusted` | Max-statistic, §13.1 |
| seed, B, strata layout, estimator and parameters | Provenance |

A p-value is never 0: its minimum is `1 / (B + 1)` (Phipson & Smyth, 2010).

### 11.6 Count

**B = 10,000** permutations for final v1 inference, plus the observed
statistic: the final permutation target of **10,001** statistics, with
p-value denominator `B + 1 = 10,001`. GCMI makes this trivial. For KSG at
n ≈ 100 it is about 10⁵ evaluations per panel, which is feasible.
Development and debug runs may use fewer permutations and must be labelled
`debug`. Only full-B runs are reported.

### 11.7 Residual nulls: Null A (fixed residual) and Null B (pipeline-aware)

**Label.** A release's *surprise label* is the pair
`(surprise_std, surprise_sign)`. The label pool is every usable release of
the family × symbol with `surprise_std_status == "ok"`. Burn-in releases
without `S` keep their raw sign and are never permuted.

**Null A, fixed residual.**

1. Compute `E` once from the observed labels, using the frozen v2 residuals.
2. Hold the residual matrix of `cs_resid` fixed.
3. Permute `S` across `cs_resid` releases within strata.
4. Compute the statistic at all horizons.

**Null B, pipeline-aware.** For each permutation:

1. Permute the labels across the label pool within strata, at release level.
2. Recompute the sign-conditioned walk-forward expected response with the
   **frozen rule**: strictly earlier usable responses of the same permuted
   sign group, min history 8, per horizon. Only information available under
   that permutation is used.
3. Recompute `E` and the residual cohort under the permutation. The cohort
   is the label-pool releases with an `ok` baseline at every horizon and a
   usable response.
4. Compute the statistic between the permuted `S` and the recomputed `E` on
   that cohort.

The observed statistic is the same function at the identity permutation.

Because Null B re-runs the whole residual construction, the statistic is a
fixed function of `(labels, responses)`. Its permutation test is exact
whenever the labels are exchangeable within strata given the responses. The
permuted cohort size can differ slightly from the observed one, and the
stored `n` is the observed cohort's. Real-data Null B must call the frozen
`src/research/baseline.add_baseline`, or a replica proven equal to it by
test, and must never modify it.

D4 (§10.3) chooses between the nulls from scenario 8 only.

---

## 12. Bootstrap uncertainty

### 12.1 Structure

- **Unit:** `release_id`. A resampled release keeps its complete horizon
  vector and its surprise.
- **Stratified:** resampling is with replacement within the §11.2 strata,
  with the same merge layout. Each stratum keeps its size.
- **Count:** 2,000 replicates for final results.
- **Primary target:** the GCMI `mi_observed_bits` profile, with each replicate
  computing all horizons. Exploratory shape summaries (§3.3) are computed per
  replicate.

### 12.2 Known issue and calibrated choice

Resampling with replacement creates duplicated releases. Duplicates add
spurious ties for rank-based GCMI and zero distances for KSG. Both inflate
dependence estimates, so a naive percentile interval is biased upward.

The **CI method** is one of the following, chosen in calibration by coverage
of the estimator's own population target (§9.3) and recorded in the
amendment:

- (a) stratified percentile bootstrap, with ties in duplicated ranks broken
  by seeded random ordering;
- (b) the same, recentred by the bootstrap-mean bias;
- (c) stratified **m-out-of-n subsampling without replacement**, with
  `m = ⌊0.7·n⌋` per stratum (Politis & Romano, 1994), rescaled by `√(m/n)`.

**Effective MI interval:** `[CI_low − mi_null_mean, CI_high − mi_null_mean]`,
using the full-sample null mean. The null bias depends mainly on n, which the
bootstrap holds fixed. A nested permutation inside each bootstrap replicate
(2,000 × 10,000) is not used. That approximation is stated with every
`MI_eff` interval.

### 12.3 KSG

The ordinary bootstrap is **not** applied to KSG automatically. KSG
uncertainty is reported as:

- the permutation null SD;
- the m-out-of-n subsampling distribution, without replacement, so there are
  no duplicates;
- the spread across k ∈ {3, 5, 10}.

A KSG bootstrap CI is added only if calibration shows acceptable coverage,
recorded in the amendment.

---

## 13. Multiple-comparison policy

### 13.1 Across horizons, within a panel: max-statistic

For each permutation `b`, compute `M_b = max_h MI_null,b(h)` over the panel's
horizon set. Then:

- `p_horizon_adjusted(h) = (1 + #{M_b ≥ MI_obs(h)}) / (B + 1)`;
- the panel-level p-value is `P_j = min_h p_horizon_adjusted(h)`.

This is single-step Westfall–Young max-T. It gives strong familywise error
control across horizons and accounts for their strong dependence.

The statistic is `MI_observed`. On common support it needs no studentization
for GCMI, because the horizon nulls are identically distributed (§8.1). For
KSG the calibration report checks the balance of per-horizon null means. If
they differ by more than 20%, the amendment (D8) switches the KSG max-statistic to
`(MI − null mean) / null SD`.

Unadjusted p-values are always reported alongside, for transparency.

### 13.2 Across the three primary panels: Holm

- Holm (1979) is applied to `{P_CPI, P_CoreCPI, P_NFP}` at FWER **α = 0.05**.
- A horizon `h` in panel `j` is declared to show *detectable dependence* only
  if:
  - panel `j` is rejected by Holm; and
  - `p_horizon_adjusted(j, h)` ≤ the Holm threshold at which panel `j` was
    rejected.

  This is a closed-testing construction: max-T within panels and Bonferroni
  intersection tests across panels. It controls FWER over all primary
  panel × horizon hypotheses.

**Why Holm and not Benjamini–Hochberg:**

1. Three confirmatory hypotheses call for FWER, not FDR. With m = 3, FDR
   control is weak protection.
2. Holm is valid under **arbitrary dependence**. The CPI and Core CPI panels
   share identical SPY response vectors and correlated surprises. BH's
   guarantee needs positive regression dependence (PRDS), which is plausible
   here but not verified.
3. With m = 3, Holm costs little power compared with BH.

### 13.3 Everything else

Secondary-raw, matched-raw, QQQ replication, available-case, exploratory
families and all sensitivity analyses report unadjusted and horizon-adjusted
p-values. They are **not** placed in the Holm family and are never described
as confirmatory discoveries.

---

## 14. Sensitivity analyses (pre-specified; complete list)

Every item is run and reported regardless of outcome. None replaces the
primary analysis, and none is chosen for presentation because it looks
better.

| # | Analysis | Comparison |
|---|---|---|
| 1 | Common support vs available case | `cs_resid` / `cs_raw` vs `ac_*(h)` (§7.3) |
| 2 | Estimator | GCMI vs KSG k = 3, 5, 10 (plus `I_lin`, Pearson, Spearman benchmarks) |
| 3 | Market | SPY vs QQQ, analysed separately; also the intersection cohort when a direct contrast is shown |
| 4 | Response type | Residual vs raw. The **like-for-like** comparison is residual vs **matched raw on `cs_resid`**. `cs_raw` vs `cs_resid` differs in sample, and this is stated wherever it is shown. |
| 5 | Permutation null | Regime-stratified vs unrestricted |
| 6 | Surprise influence | Remove the one release with the largest `|S|` in the cohort, at **all** horizons, which keeps common support |
| 7 | Response influence | Remove the one release maximising `max_h |X_h − median(X_h)| / MAD(X_h)`, with `X = E` for residual and `X = R` for raw, at all horizons |
| 8 | Session boundary | Pre-open profile and labelling (§15) |

Sensitivities 1, 3 and 5–8 use the primary estimator. Sensitivity 2 covers
all estimators on the primary cohorts.

---

## 15. Structural market-open caveat

All primary releases in the cohorts are at **08:30 ET** (verified: 93/93/103
of the SPY raw cohorts). For these releases:

- Horizons 1–30m are entirely **pre-market**, ending at 08:59 ET.
- The **60m horizon** covers bars 08:30–09:29. Its end price is the close of
  the 09:29 bar, the last pre-market price **at the 09:30 regular-session
  boundary**. It does *not* include the opening-auction bar, but it does span
  the pre-open half hour, when liquidity, order imbalance publication and
  price discovery change structurally.
- The 60m completeness requirement is the binding cohort constraint in early
  years. Of the 34 SPY CPI exclusions, 24 are 60m
  `insufficient_market_window`.

Rules:

1. A change between 30m and 60m is **never** interpreted as pure information
   decay or growth. In every table and figure, the 60m point is labelled
   `session-boundary-adjacent`.
2. **Pre-open sensitivity (§14, item 8):**
   - the profile on `cs_resid_preopen` / `cs_raw_preopen` (horizons ≤ 30m),
     with its own horizon-adjusted max-statistic over the pre-open horizons
     only;
   - the canonical cohort's profile with the 60m point shown separately.
3. The 60m horizon stays in the canonical experiment. v1 does not remove it.
4. Exploratory families with 10:00 releases (Core PCE) are restricted to
   08:30 releases (§4.2), so the horizon meaning stays consistent.

---

## 16. Interpretation boundaries

1. **Dependence, not causation.** MI measures statistical dependence. These
   phrasings are allowed: *statistical dependence*, *information association*,
   *detectable dependence*, *information profile*, *consistent with
   rapid/slow incorporation*. These are forbidden: "the surprise causes X
   bits", "MI proves market absorption", "information disappears because the
   market is efficient".
2. **Raw vs residual.**
   - `I(S; R_h)` measures how statistically dependent the observed market
     response at horizon h is on the macro surprise.
   - `I(S; E_h)` measures how statistically dependent the response that
     remains after subtracting the existing walk-forward sign-group baseline
     is on the macro surprise.
   - The latter is **not** "new information after conditioning on everything
     the market knew" (§2, §19.3).
3. **GCMI is monotonic information.** A GCMI value is a lower bound under the
   Gaussian-copula assumption. "No GCMI dependence" does not mean "no
   dependence" (§8.1).
4. **Retrospective historical inference.** The development / validation /
   test labels were built for walk-forward evaluation of the response
   baseline. M3 uses them only as **regime strata** for permutation and
   bootstrap. Full-history MI is labelled `retrospective historical
   inference`. It is never called out-of-sample prediction or held-out
   validation, and the protocol makes no claim of prospective validation.
5. **Baseline walk-forward evaluation ≠ MI dependence estimation.** The
   baseline residual is walk-forward because each `Rhat` uses strictly earlier
   data. The MI between `S` and that residual is estimated on the whole
   cohort at once.
6. **Point-in-time caveat inherited from v2.** Forecasts come from
   retrospectively saved calendar pages and none is independently verified as
   point-in-time. Measured MI is MI with *this* surprise measure.
7. **Selection.** Common support needs every pre-market minute to trade, so
   cohorts lean toward liquid, later-year releases. Results describe this
   cohort. The available-case sensitivity and the `|S|` selection diagnostic
   (§7.4) bound, but cannot remove, the selection effect.
8. A negative `MI_eff` is reported as is and read as "no detectable
   information", never as "negative information".

---

## 17. Reproducibility and seeds

**No global RNG stream.** Every stochastic procedure gets its own generator,
derived from a stable identifier and independent of loop order:

```
key  = "m3_information_decay|v1|{procedure}|family={F}|cohort={cohort_id}|stratification={policy}"
       [ + "|symbol={SYM}|response={raw|resid}|estimator={EST}" where the procedure is symbol/estimator-specific ]
seed = int.from_bytes(sha256(key.encode("utf-8")).digest()[:8], "big")
rng  = numpy.random.Generator(numpy.random.PCG64(numpy.random.SeedSequence(seed)))
```

- `cohort_id` = the first 16 hex characters of the SHA-256 of the sorted,
  newline-joined `release_id`s in the cohort. Permutations depend on the
  releases, not on row order.
- **Permutation** keys contain *no* horizon, estimator, symbol or response
  type. One permutation set per (family, cohort, stratification) is shared by
  every horizon (needed for max-T), every estimator, raw and matched-raw, and
  both symbols on an intersection cohort.
- **Bootstrap / subsampling** keys follow the same rule
  (`procedure = bootstrap|subsample`).
- **KSG tie-jitter** keys add symbol, response type and `estimator=ksg_k{k}`.
  The jitter is drawn once, before permutation.
- **Calibration** keys use `procedure=calibration|scenario={s}|n={n}|rep={r}`.
- Every output records: key, seed, B, strata layout, estimator and
  parameters, the input dataset content hash, the provenance file hash, git
  HEAD and a dirty flag. This follows the convention of
  `metadata/research/event_response_v2.json`.

---

## 18. Planned output schema (frozen; not implemented)

**Profile table:** one row per `family × symbol × response_type × cohort ×
estimator × horizon`.

| Column | Meaning |
|---|---|
| `experiment_id`, `analysis_role` | `m3_information_decay_v1`; `primary` / `secondary` / `replication` / `exploratory` / `sensitivity:{id}` |
| `event_family`, `symbol`, `response_type` | `resid` / `raw` / `matched_raw` / `exploratory_raw_only_2m` |
| `cohort_id`, `cohort_kind` | `cs_resid`, `cs_raw`, `ac`, `preopen` |
| `estimator`, `estimator_params` | e.g. `gcmi` with `{}`, or `ksg` with `{k: 5}` |
| `horizon_min`, `session_label` | `premarket` / `session-boundary-adjacent` |
| `n` | Releases at this point (constant along a common-support curve) |
| `mi_observed_bits` | Estimate on real pairing; the brief's `MI_raw_bits`, renamed to avoid collision with raw-response MI |
| `mi_null_mean_bits`, `mi_null_sd_bits` | Permutation null |
| `mi_effective_bits` | Not truncated |
| `p_unadjusted` | The brief's `p_raw`, renamed for the same reason |
| `p_horizon_adjusted` | Max-T |
| `p_holm_panel`, `primary_detectable` | Primary rows only (§13.2) |
| `ci_low_bits`, `ci_high_bits`, `ci_method`, `ci_target` | `ci_target` ∈ {`observed`, `effective`} |
| `pearson`, `spearman`, `p_pearson`, `p_spearman`, `i_lin_bits` | Benchmarks |
| `B_perm`, `B_boot`, `perm_seed`, `boot_seed`, `strata_layout` | Provenance |

**Panel-summary table**, one row per panel:

- `I_peak`, `h_peak`, and the bootstrap frequencies of `h_peak`;
- retention at each post-peak horizon;
- `h_below_50pct_of_peak`;
- `P_j`, its Holm-adjusted value and the Holm rank;
- every shape summary flagged `exploratory`.

**Pre-MI count tables:** as in §7.2, plus the `|S|` selection diagnostic.

**Metadata JSON** in the v2 provenance style: input hashes, implementation
state, amendment ID in force, and output content hashes.

**Figures:** the primary figure plots `mi_effective_bits` against the horizon
with its CI, overlays `mi_observed_bits` and the null mean, and marks the 60m
point as session-boundary-adjacent. The horizon axis is categorical or
log-spaced, never interpolated between horizons.

---

## 19. Future work: information freshness (AoI / AoII) and conditional MI

Nothing in this section is a v1 result or a v1 term.

### 19.1 Conceptual bridge

At `t = 0` an exogenous innovation, the surprise `S`, becomes public. The
profile `h ↦ I(S; R_h)`, and especially `h ↦ I(S; E_h)`, is an empirical
measure of **how much statistical information about that original innovation
remains observable in the market response at age h**. That makes it a
natural empirical companion to information-freshness metrics:

- **Age of Information (AoI)** (Kaul, Yates & Gruteser, 2012; Kosta, Pappas &
  Angelakis, 2017) measures the time since the latest update was generated.
  Here the release time fixes the "age" of the macro information at a
  horizon.
- **Age of Incorrect Information (AoII)** (Maatouk et al., 2020) penalises
  the time a monitor's estimate stays *wrong*. One market analogue is the
  time during which the price has not yet incorporated the surprise. That
  needs a defined "correct" post-release state, which v1 does not have.

### 19.2 Possible later definitions (each needs a mathematical definition and an amendment first)

- state-dependent value of information, `I(S; R_h | state)`;
- value-of-information decay, a parametric or nonparametric shape fitted to a
  validated profile;
- an information-age function `A(h)`, tied to the profile;
- AoII analogues with an explicit incorrectness criterion.

### 19.3 Conditional MI: reserved for M4 or later

`I(S; R_h | Z_pre)`, with `Z_pre` drawn from:

- pre-event volatility, return and volume (the frozen `pre*` windows exist);
- regime or state variables;
- event expectations;
- other simultaneous macro surprises, at release level.

Not implemented in v1. Until then, `E_h`-based MI must not be described as
conditional MI.

---

## 20. Explicit frozen decisions

| # | Decision |
|---|---|
| F1 | Residual MI `I(S; E_h)` is the intended primary quantity (effective-MI profile), **conditional on D4**. Raw MI is secondary. Matched raw on `cs_resid` at the same five horizons is the primary raw-versus-residual comparator. |
| F2 | Profile horizons are `{1, 5, 15, 30, 60}` for residual, matched raw and secondary raw (**D0 ratified**). The raw 2m point is exploratory only, outside every profile and family. |
| F3 | Primary families CPI_MOM, CORE_CPI_MOM and NFP; exploratory tier as in §4.2; no FOMC MI |
| F4 | SPY primary; QQQ replication/exploratory, never pooled; intersection cohort for joint analyses |
| F5 | `release_id` is the resampling and permutation unit; linked components across families |
| F6 | Common-support primary cohort (§7.1); available case is sensitivity only; no relaxation to raise n |
| F7 | Units: bits |
| F8 | Release-level permutation, stratified 2016–2020 / 2021–2022 / 2023-01 → 2026-09 with the merge rule (§11.2); B = 10,000 (10,001 statistics, denominator 10,001); `(1 + count) / (B + 1)`; residual null per D4 (§11.7) |
| F9 | `MI_eff = MI_obs − null mean`, untruncated |
| F10 | Horizon-wise max-T within a panel; **Holm** across the 3 primary SPY panels at α = 0.05; closed-testing horizon claims (§13.2) |
| F11 | Stratified release bootstrap, 2,000 replicates; candidate interval methods fixed (§12.2, D5) |
| F12 | KSG k ∈ {3, 5, 10} as a robustness family, never tuned per panel |
| F13 | Sensitivity list §14 (8 items), complete and run unconditionally |
| F14 | No monotonic-decay assumption, no exponential fit in v1, no "half-life" |
| F15 | Market-open labelling and pre-open sensitivity (§15) |
| F16 | Seed-derivation scheme (§17) |
| F17 | Calibration design (§9), decision rules (§10.3), threshold rule (§10.4) |
| F18 | Output schema (§18) |
| F19 | Interpretation boundaries (§16), including no causal interpretation |
| F20 | No estimator, threshold, tie policy, interval method or null is selected from real-data results |

## 21. Decisions: ratified, and still requiring synthetic evidence

| # | Decision | Status / resolved by | Prior (not a required outcome) |
|---|---|---|---|
| **D0** | Residual profile on {1, 5, 15, 30, 60}; matched raw on the same releases and horizons; raw 2m exploratory only | **RATIFIED** by the project owner | — |
| D1 | Primary estimator | §10.2–10.3, simulation only | GCMI |
| D2 | `n_min` and panel eligibility | §10.4 | 60 (provisional) |
| D3 | Tie policy (GCMI A/B/C; KSG K-A/K-B) | Scenario 9, §10.3 | A / K-A |
| D4 | Residual primacy and the residual null (Null A vs Null B) | Scenario 8, §10.3 | Residual primary; Null B may be needed |
| D5 | Interval method for the primary estimator, (a)/(b)/(c) | Coverage, §10.3 | — |
| D6 | Synthetic surprise-standardization policy (mode A direct vs mode B pipeline-faithful) | §10.3 | — |
| D7 | KSG uncertainty policy (bootstrap / subsampling / no formal CI) | Coverage, §10.3 | No formal CI |
| D8 | KSG max-statistic studentization | Scenario 1h, §10.3 | Unstudentized |

**Amendment procedure.** The calibration results produce
`docs/research/information_decay_v1_amendment_01.md`. It records D1–D8 with
the simulation evidence, the calibration config hash, this document's freeze
commit and the output hashes. It is written **before** any real-data MI code
is run. Each amendment states whether real-data MI had been computed when it
was written. For amendment 01 the answer must be "no".

---

## Pre-analysis Freeze

The following are **frozen** by the commit
`Freeze M3 information-decay methodology`, which precedes any real-data MI:

- [x] SPY is the primary market; QQQ is replication/exploratory; never
      pooled.
- [x] Primary families: CPI m/m, Core CPI m/m, NFP. FOMC is excluded from
      MI. Exploratory tier as listed.
- [x] Residual MI is the intended primary quantity, subject to D4. Raw MI is
      secondary. Matched raw uses the exact residual cohort and the same five
      horizons.
- [x] Residual horizons are 1/5/15/30/60 (D0 ratified). Raw 2m is
      exploratory only.
- [x] Units in bits.
- [x] Common-support primary cohort; available case is sensitivity only.
- [x] `release_id` is the resampling and permutation unit.
- [x] Chronological permutation strata 2016–2020 / 2021–2022 /
      2023 → 2026-09, with the merge rule.
- [x] Final permutation target 10,001 (B = 10,000 plus the observed
      statistic); final bootstrap target 2,000.
- [x] Horizon-wise max-stat correction; Holm across the primary family.
- [x] No monotonic-decay assumption; no exponential decay fit in v1.
- [x] No causal interpretation.
- [x] No real-data estimator selection.
- [x] Calibration procedure: scenarios (§9.2), metrics, acceptance region,
      decision rules D1–D8, `n_min` rule.
- [x] Sensitivity-analysis list (§14); seed scheme; output schema;
      market-open labelling.

**Conditionally open until amendment 01:**

- estimator choice (D1);
- minimum n (D2);
- tie handling (D3);
- residual null (D4);
- bootstrap CI method (D5);
- standardization policy (D6);
- KSG uncertainty (D7);
- KSG studentization (D8).

**Sequencing:**

1. Freeze this document.
2. Run the synthetic-only calibration. It reads no real surprise, response or
   residual values.
3. Write amendment 01.
4. Review.
5. Only then implement and run the real-data MI pipeline.

---

## References

Primary estimator literature:

- Kraskov, A., Stögbauer, H., & Grassberger, P. (2004). Estimating mutual
  information. *Physical Review E*, 69(6), 066138.
- Ince, R. A. A., Giordano, B. L., Kayser, C., Rousselet, G. A., Gross, J., &
  Schyns, P. G. (2017). A statistical framework for neuroimaging data analysis
  based on mutual information estimated via a Gaussian copula. *Human Brain
  Mapping*, 38(3), 1541–1573.

Supporting references. These are cited from the author's knowledge, with no
network access during drafting. Verify the bibliographic details before any
external publication.

- Cover, T. M., & Thomas, J. A. (2006). *Elements of Information Theory* (2nd
  ed.). Wiley.
- Paninski, L. (2003). Estimation of entropy and mutual information. *Neural
  Computation*, 15(6), 1191–1253. (Finite-sample MI bias.)
- Gao, W., Kannan, S., Oh, S., & Viswanath, P. (2017). Estimating mutual
  information for discrete-continuous mixtures. *NeurIPS 30*.
- Ross, B. C. (2014). Mutual information between discrete and continuous data
  sets. *PLoS ONE*, 9(2), e87357.
- Phipson, B., & Smyth, G. K. (2010). Permutation p-values should never be
  zero. *Statistical Applications in Genetics and Molecular Biology*, 9(1).
- Westfall, P. H., & Young, S. S. (1993). *Resampling-Based Multiple Testing*.
  Wiley.
- Holm, S. (1979). A simple sequentially rejective multiple test procedure.
  *Scandinavian Journal of Statistics*, 6(2), 65–70.
- Benjamini, Y., & Hochberg, Y. (1995). Controlling the false discovery rate.
  *JRSS B*, 57(1), 289–300.
- Benjamini, Y., & Yekutieli, D. (2001). The control of the false discovery
  rate in multiple testing under dependency. *Annals of Statistics*, 29(4),
  1165–1188.
- Politis, D. N., & Romano, J. P. (1994). Large sample confidence regions
  based on subsamples under minimal assumptions. *Annals of Statistics*,
  22(4), 2031–2050.
- Andersen, T. G., Bollerslev, T., Diebold, F. X., & Vega, C. (2003). Micro
  effects of macro announcements: Real-time price discovery in foreign
  exchange. *American Economic Review*, 93(1), 38–62. (Standardized
  announcement surprises.)
- Kaul, S., Yates, R., & Gruteser, M. (2012). Real-time status: How often
  should one update? *IEEE INFOCOM*.
- Kosta, A., Pappas, N., & Angelakis, V. (2017). Age of information: A new
  concept, metric, and tool. *Foundations and Trends in Networking*, 12(3).
- Maatouk, A., Kriouile, S., Assaad, M., & Ephremides, A. (2020). The age of
  incorrect information: A new performance metric for status updates.
  *IEEE/ACM Transactions on Networking*, 28(5).
