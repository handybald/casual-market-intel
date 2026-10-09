# M4 Research Problem Definition: Information Freshness and Predictive Value of Macroeconomic Surprises

| Field | Value |
|---|---|
| Milestone | M4: research problem definition, **revision 3** (after the second independent design review) |
| Status | **Design draft for second review.** Nothing is implemented, estimated, trained, calibrated or run. The document contains no real-data result and no synthetic result. |
| Builds on | `m3-integrated-research-v1` → `c1c42b623a51425d36c187b149a0360124082bbd` (frozen M3 integrated report) |
| Does not modify | Any frozen M2 or M3 artifact, methodology, dataset, configuration or result |

> **Scope guard.** Every hypothesis below is *open*. Every "recommended"
> choice is a **design proposal**, not a validated or calibrated method.
> Where M3 is cited, it is cited only for what it established about
> **dependence with cumulative responses**. No M3 inference is evidence for
> or against any M4 hypothesis.

**Revision 3 changes** (targeted; other sections unchanged from revision 2).

- The N1–N15 classification follows the review's decision-level labels
  (§N).
- The primary feature set is fixed at exactly three features (§D.5).
- Surprise availability is an explicit idealized assumption, with the
  relevant timestamps distinguished (§D.4).
- The shared training history is clarified (§E).
- Any fit failure now aborts the run (§G).
- H1 is the only confirmatory family. H2 is a planned secondary question
  and is not tested in the initial experiment (§I.2, §K.4).
- Calibration timing is aligned between §G, §K and §N.

**Revision 2 changes.**

- The estimand is now algorithm-, target- and population-specific (§F).
- Information-theory statements are corrected (§F.3, §M).
- The primary loss is fixed (§F.2).
- Ridge fairness rules are added (§G).
- Three grouping units are defined (§D.8).
- Timing, exception precedence and training-cutoff rules are added (§D.2,
  §D.6, §J).
- Retrospective, holdout and prospective validation are separated (§K.1).
- H1, H2 and H3 are redefined, with the multiplicity and gating structure
  (§I, §K). The gating is superseded in revision 3: H2 is not tested
  initially.
- An age-confounding section (§H.3), the calibration requirements, the
  stop/go gate (§K.5–K.6) and the A/B/C decision matrix (§N) are added.

---

## A. Motivation

M3 measured how strongly a public macroeconomic surprise is statistically
associated with the **cumulative** equity response from just before the
release. M4 asks a different question: **at a fixed time after the release,
does adding the explicit surprise to the market information then observable
lower the out-of-sample loss of a specified forecasting procedure for the
next, non-overlapping return?**

Four quantities must be kept distinct. They are **not** equivalent, and
none implies another.

| # | Quantity | Typical form | What it answers | Studied in |
|---|---|---|---|---|
| 1 | Dependence with cumulative responses | `I(S; R_h)`, `R_h` = frozen M3 simple return from the pre-release reference close to the end of horizon h | Is the surprise associated with the total price move up to h? | M3 (frozen) |
| 2 | Predictive information about future returns | Model-free: `I(S; Y \| Z)`. Algorithm-specific: `ΔL` (§F) | Given the market information observable at decision age a, does S carry information about, or improve a specified forecast of, the return after a? | M4 (ΔL is the initial endpoint) |
| 3 | Statistical information freshness | How quantity 2 varies with decision age a | Does incremental predictive content differ across ages? This is age-indexed and confounded (§H.3). | Planned secondary question (H2); **not tested** in the initial M4 experiment |
| 4 | Economic value of information | Expected net payoff of a pre-specified decision rule and cost model | Would a decision-maker be better off, net of costs? | Later phase (H3) |

**Why these quantities are not interchangeable.**

- **1 does not imply 2.** If prices respond only in the first minute,
  `I(S; R_h)` remains positive at every h, because each cumulative return
  contains that minute. Yet S may tell us nothing about the returns after
  the first minute.
- **Model-free information is not algorithmic improvement.** `I(S; Y | Z)`
  may be positive while a specific estimated procedure fails to lower
  out-of-sample loss, and the reverse can occur with finite-sample noise.
- **Neither is trading profit.** Neither `I(S; Y | Z)` nor `ΔL` implies
  positive cost-adjusted economic value.

---

## B. Established M3 evidence

These statements come from the frozen M3 integrated report (§9, §11 and
§14). They are repeated, not reinterpreted.

**Established (SUPPORTED in the M3 claim audit).** All of the following
concern dependence with cumulative responses `R_h` over overlapping
windows:

- Macro-surprise/market-response dependence is detectable for CPI-family
  events in SPY and QQQ under the frozen procedure.
- No corresponding horizon-adjusted dependence was detected for NFP under
  the tested procedures. This is non-detection only.
- Residualization substantially changes the observed dependence profile,
  with some short-horizon CPI association remaining.
- Panel-level CPI-family detection is robust to the estimator.

**Not established by M3:**

- statistical information decay;
- estimator-independent temporal profiles;
- any predictive information about returns after h;
- information freshness, decision value or value of information;
- residual GCMI as conditional MI;
- causal effects;
- that NFP has no market effect.

**Consequence for M4.** M3 provides **no evidence about M4 predictive
value**, in either direction:

- M3's CPI-family detection is not evidence for H1;
- M3's NFP non-detection is not evidence against H1 for NFP;
- M3's descriptive profile shapes must not be used to choose ages,
  families, features, models or tests.

M3 has, however, *examined* the whole historical period, which limits what
the retrospective M4 evaluation can claim (§K.1).

---

## C. M4 research question

> For a scheduled macroeconomic release and a decision made a fixed number
> of minutes after it, does a specified ridge forecasting procedure that is
> given the explicit standardized surprise, in addition to market-only
> features, achieve lower expected out-of-sample squared-error loss for the
> subsequent 5-minute return than the same procedure without the surprise?

The question of whether that algorithm-specific improvement differs across
decision ages (H2) is a **planned secondary question**. It is **not tested**
in the initial experiment (§I.2).

| Item | Initial scope (design proposal) |
|---|---|
| Families | CPI m/m, Core CPI m/m, NFP. NFP is retained regardless of M3. |
| Assets | SPY (primary confirmatory); QQQ as a separate, secondary replication |
| Decision ages | a ∈ {1, 5, 15, 30} minutes |
| Prediction horizon | Δ = 5 minutes, always the full window (§D.6) |
| Primary endpoint | Raw-return MSE difference `ΔL_MSE` (§F.2) |
| Secondary endpoint | MAE difference on exactly the same predictions (descriptive sensitivity) |
| Confirmatory inference | H1 only: 12 SPY family × age hypotheses, Holm (§I.1) |
| Age variation (H2) | Planned; not tested initially (§I.2) |
| Economic value | Out of scope for the first experiment (H3) |

---

## D. Mathematical definitions

### D.1 Releases, families and timestamps

- `i` indexes a **release**: one occurrence of family
  `f(i) ∈ {CPI_MOM, CORE_CPI_MOM, NFP}`.
- `s ∈ {SPY, QQQ}` indexes the symbol.
- `T_i` is the **scheduled release timestamp**: the frozen M2 field, stored
  as a timezone-aware instant at **minute precision**. No seconds are
  invented.
- `τ_i = ⌊T_i⌋_min` is the scheduled release minute. All initial-scope
  releases are scheduled at 08:30 America/New_York.
- `T_i^pub` is the **actual publication instant**. It is **not recorded**
  in the historical dataset; scheduled and actual timestamps are distinct
  quantities.
  - The historical design assumes punctual publication within the
    scheduled minute: `T_i^pub ∈ [τ_i, τ_i + 1 min)` (assumption **A-pub**).
  - A-pub cannot be verified historically.
  - Prospective capture is to record publication timestamps where
    available (§K.1).
  - A-pub concerns the release event only. It does **not** establish when
    the recorded values became available to anyone (§D.4).

### D.2 Bars, completion and availability

These rules inherit the frozen M2 canonical bar semantics and are not
changed by M4.

- `b_i(k)` is the canonical Alpaca SIP 1-minute bar of symbol s that starts
  at `τ_i + k` minutes. It covers `[τ_i + k, τ_i + k + 1)`.
- `C_i(k)` is its close.
- `b_i(0)` is the release bar. It may contain seconds of pre-publication
  trading. `b_i(−1)` is the reference bar.
- **Scheduled completion.** Bar `b_i(k)` is complete at `τ_i + k + 1`. It
  is **feature-eligible** at decision time t only if `τ_i + k + 1 ≤ t`.
- **Completion is not delivery.** A completed bar is not proof that the
  bar had reached the researcher by that time.
  - The historical design adopts **zero delivery latency**. This is an
    idealized retrospective assumption, not a fact about any real-time
    system.
  - Historical bars may also include late-reported or corrected trades that
    a real-time observer would not have seen at t. This is a bar-vintage
    effect; it is not corrected historically and is to be recorded
    prospectively.
- **Existence.** A bar exists only if eligible trades occurred in its
  minute. **A missing bar is never filled, interpolated or treated as a
  zero return or zero volume.**
- **Price at an instant.** `P_i(k) := C_i(k − 1)`, which is defined only if
  `b_i(k − 1)` exists.

### D.3 Decision time and information age

- **Decision time:** `t_i(a) = τ_i + a` minutes, for a ∈ {1, 5, 15, 30}.
- **Age at decision time:** `A_i(t) = t − T_i^pub`.
  - Under A-pub, the nominal age a is used throughout.
  - The true age satisfies `A_i(t_i(a)) ∈ (a − 1, a]` minutes. This
    sub-minute uncertainty is a limitation, not data.
- All initial decision and target windows lie in the pre-market session.
  The last target bar ends at `τ_i + 35` = 09:05 ET, before the 09:30 open.

### D.4 Surprise

`S_i` is the frozen M2 `surprise_std`:

`S_i = (x_i − μ_{<i}) / σ_{<i}`, with `x_i = actual_i − forecast_i`,

where `μ_{<i}` and `σ_{<i}` (ddof = 1) come only from strictly earlier
usable releases of the same family, with at least 12 of them.

- **Vintage.** The forecasts come from retrospectively saved calendar pages
  and are not point-in-time verified (§J.7).

**Distinct timestamps and vintages.** None of these may be substituted for
another.

| Quantity | Meaning | Historical status |
|---|---|---|
| Scheduled release minute `τ_i` | The calendar's scheduled minute | Recorded (minute precision) |
| Actual publication timestamp `T_i^pub` | When the agency actually published | **Not recorded**; assumed within `τ_i` (A-pub) |
| Source acquisition timestamp | When the recorded actual and forecast values were obtained by the data source or the researcher | **Not recorded** in real time. The calendar pages were saved retrospectively. |
| Forecast vintage | Which consensus snapshot the recorded forecast represents, and when it was taken | **Unknown / not point-in-time verified** |
| First published actual | The initially released value, before revisions | **Not verified** for most releases (M3 report §3) |
| Delivery latency | Delay between publication and receipt by a decision-maker | **Not modelled** |

**Assumption A-avail (idealized availability).** For the retrospective
statistical experiment, the recorded `S_i` is **treated as available** at
every eligible decision time `t_i(a)` with a ≥ 1. This is an explicit
idealized assumption. A-pub does **not** imply it: A-pub concerns the
release minute, while A-avail concerns the recorded value being known in
real time at t. **Historical real-time availability of the recorded `S_i`
is not verified.** The retrospective experiment therefore claims no
point-in-time correctness for `S_i`.

### D.5 Market-only features

`Z_i(a)` is a fixed, finite vector computed **only** from feature-eligible
bars of symbol s at `t_i(a)` and from strictly earlier sessions.

**Constraint:** no component of `Z_i(a)` is a function of `S_i`, of
`actual_i` or `forecast_i`, or of any surprise, actual or forecast
belonging to the same economic-release cluster (§D.8).

**Primary feature set** (frozen, §N3). There are exactly three features.
Writing `C_k` for `C_i(k)`, the close of `b_i(k)`, which starts at
`τ_i + k`:

| Feature | Definition | Bar closes required |
|---|---|---|
| `Q_i`, pre-release 30-minute simple return | `Q_i = C_{−1} / C_{−31} − 1` | `C_{−31}, …, C_{−1}` (31 closes) |
| `U_i`, pre-release one-minute return dispersion | `U_i = sample_std(r_{−30}, …, r_{−1})` with ddof = 1, where `r_j = C_j / C_{j−1} − 1` for `j = −30, …, −1` (30 completed one-minute simple returns) | `C_{−31}, …, C_{−1}` (31 closes) |
| `M_i(a)`, cumulative post-release move to the decision-time anchor | `M_i(a) = C_{a−1} / C_{−1} − 1` | `C_{−1}, …, C_{a−1}`: every bar `b(−1) … b(a−1)` |

**Validity of the feature windows.**

- `Q_i` and `U_i` share the pre-release window `b(−31) … b(−1)` and need
  **all 31 bar closes**.
- `M_i(a)` needs every bar from `b(−1)` to `b(a−1)`, although its formula
  uses only the two endpoints.
- Every underlying bar must be valid under §D.6. No missing bars, no
  interpolation and no shortened feature windows are allowed.

**Model dimensions.**

- The **F0 model has three regressors** `(Q_i, U_i, M_i(a))` plus an
  intercept.
- The **F1 model has the same three regressors plus `S_i`**, plus an
  intercept.

**Excluded from the initial primary specification:**

- volume features;
- post-release realized volatility;
- any additional technical indicator;
- nonlinear feature transformations.

`Q_i` and `U_i` do not depend on age. `M_i(a)` coincides with the M3
cumulative response `R_a`; here it is a predictor **input** available at
t, not a target. The one-minute returns `r_j` are feature-construction
notation, distinct from M3's `R_h` and from the log returns `x(k)` of §M.

### D.6 Target and window validity

The target for age a is the **full** 5-minute simple return after the
decision:

`Y_i(a) := Y_i(t_i(a), 5) = P_i(a + 5) / P_i(a) − 1 = C_i(a + 4) / C_i(a − 1) − 1`

**Window validity rules** (M4 rules, applied identically to every feature
window and every target window). A window, whether a target window
`[b(a−1) … b(a+4)]` or a feature window, is **valid** only if every rule
passes. The rules are checked in this precedence, and the first failure
gives the recorded exclusion reason:

1. **Provisional data.** The window ends after the research end date
   (2026-09-30 for the frozen historical dataset) → `provisional`.
2. **Provider defect.** Any registered `provider_gap` or
   `temporary_fetch_failure` entry intersects the window →
   `provider_gap_in_window`.
3. **Exchange closed.** Any registered `exchange_closed` entry intersects
   the window → `exchange_closed`.
4. **Halt or no-trade interval.** Any registered `market_wide_halt` or
   `legitimate_no_trade_interval` entry intersects the window →
   `halt_or_no_trade_in_window`.
5. **Mandatory bars.** The anchor bar, the endpoint bar or any intermediate
   bar is missing → `insufficient_market_window`.

**No silent target shortening.** The frozen M2 window rule drops registered
halt and no-trade minutes from the expected bar set, and accepts such
windows (`ok_market_halt`). **M4 deliberately does not inherit that
acceptance.** A target or feature window that intersects a registered halt,
closure or no-trade interval is excluded (rules 3–4), so every valid target
covers exactly five traded clock minutes with all bars present.

This is an M4 rule. It does not alter the frozen M2 layer or its outputs.

### D.7 Panel cohorts

A panel is a family f × symbol s × age a. For each (f, s) and age a, the
age-a cohort `𝒞_{f,s}(a)` contains release i only if all of the following
hold:

- `S_i` is usable;
- there is no M2 event exclusion;
- every window feeding `Z_i(a)` is valid;
- the target window for `Y_i(a)` is valid.

The **common-support cohort** is `𝒞*_{f,s} = ∩_a 𝒞_{f,s}(a)`. The design
proposal is to use it for every age, so that age comparisons use identical
releases (§N4).

### D.8 Grouping units

| Unit | Definition | Role |
|---|---|---|
| **Panel observation** | `o = (i, f, s, a)`: one release × family × symbol × age, with inputs `(Z_i(a), S_i)` and target `Y_i(a)` | Row of a training or evaluation set |
| **Economic-release cluster** | `c(o)` = the scheduled release instant `T_i` expressed in **UTC**. All panel observations sharing it form one cluster: both CPI-family events of the same release, every age, both symbols, and any other in-scope family scheduled at the same instant. | The unit of chronological splitting, ordering and dependence |
| **Evaluation unit** | The paired out-of-sample loss difference `d_o` (§F.1) of one evaluation panel observation | Input to inference, which must account for the dependence of `d_o` within and across clusters |

**Rules.**

- **Splits.** Every panel observation of one cluster sits on the same side
  of every split and fold. Training histories contain only clusters
  **strictly earlier** than the predicted cluster (§J.1).
- **Dependence.** Observations within a cluster share prices, the release
  instant, and for CPI and Core CPI the same targets. Clusters may also be
  dependent across time through regimes. **An iid bootstrap or permutation
  over releases is not assumed valid**; candidate procedures must be
  cluster-aware and calibrated (§K.3).

---

## E. Information sets

`𝒟_{f,s,a}(c)` is the training history for cluster c. It contains the
tuples `(Z_j(a), S_j, Y_j(a))` of panel (f, s, a) for which:

- `c(j) < c` strictly;
- `j ∈ 𝒞*_{f,s}`;
- the target window of j was **complete before the training cutoff** `c`:
  `τ_j + a + 5 ≤ c`, which earlier clusters always satisfy.

The sets are:

- `F0_i(a) = σ( Z_i(a), 𝒟_{f,s,a}(c(i)) )`: market-only information;
- `F1_i(a) = σ( Z_i(a), S_i, 𝒟_{f,s,a}(c(i)) )`: the same plus the
  explicit surprise.

By construction `F0_i(a) ⊆ F1_i(a)`.

**Shared training history.**

- F0 and F1 use **the same eligible historical economic-release
  observations** `𝒟_{f,s,a}(c)`. They may read the same underlying training
  dataset, but their model-input projections differ:
  - **F0** fits on the historical `Z` columns `(Q_j, U_j, M_j(a))` and
    `Y_j(a)` only;
  - **F1** fits on the same `Z` columns plus the historical `S_j`, and the
    same `Y_j(a)`.
- **At evaluation,** F0 does **not** use the current `S_i`. F1 receives the
  current `S_i` under assumption A-avail (§D.4).
- **No other asymmetry is permitted** between the two procedures: not in
  observations, targets, preprocessing, tuning or failure handling.

**Interpretation.**

- **F0 is not "the market without the announcement".** `Z_i(a)` contains
  the post-release price path. Trading during that path may already reflect
  the announcement, so information about S may be partly present in F0
  **through prices**.
- **Excluding S from F0 is a restriction on the predictor's inputs.** It is
  not a statement about what the market knows.
- **What an F1 versus F0 comparison measures.** It measures the incremental
  value of the explicit surprise **for a specified algorithm, given a
  specified feature set**. Consider a non-positive `ΔL`. It is consistent
  with each of the following, and the comparison cannot distinguish them:
  - the announcement's information is already reflected in `Z`;
  - S was never predictive of `Y`;
  - S is predictive, but the procedure cannot exploit it at the available
    sample size.
- **Other simultaneous macro information.** The other CPI-family surprise
  of the same cluster, forecast levels and previous values are **excluded
  from both** F0 and F1 in the initial design (§N10).

---

## F. Predictive-value functional

### F.1 Estimand

- **Algorithm.** `𝒜` is a fully specified training-and-tuning algorithm
  (§G), including preprocessing, the hyperparameter search and
  fit-failure handling. It maps a training set and an input specification
  `φ` to a predictor.
- **Input specifications.** `φ₀(o) = Z_i(a)` and
  `φ₁(o) = (Z_i(a), S_i)`.
- **Predictors** for evaluation observation o:
  - `ŷ₀(o) = 𝒜(𝒟(c(o)), φ₀)(φ₀(o))`;
  - `ŷ₁(o) = 𝒜(𝒟(c(o)), φ₁)(φ₁(o))`.
- **Paired loss difference** for loss ℓ:
  `d_o = ℓ(Y_o, ŷ₀(o)) − ℓ(Y_o, ŷ₁(o))`.

**Estimand.**

`ΔL_{f, s, 𝒜, ℓ, 𝒫}(a; Y) = E_𝒫[ d_o ] = E_𝒫[ ℓ(Y, ŷ₀) ] − E_𝒫[ ℓ(Y, ŷ₁) ]`

- The expectation is over the **evaluation population** `𝒫`. That is the
  data-generating process of panel observations (f, s, a) in a specified
  evaluation period and eligibility rule, **together with** the training
  histories that the specified walk-forward scheme produces for them.
- **Sign.** Positive values mean that the F1 procedure has lower expected
  prediction loss than the F0 procedure.
- **Retrospective evaluation population (design proposal):** common-support
  observations whose cluster date falls in the frozen M2 test period,
  2023-01-01 to 2026-09-30. Each is predicted by expanding-window
  walk-forward from the 2016 start (§N5–N6). Because markets are not
  stationary, the estimand is **specific to that period**.
- **Sample estimator:** `ΔL̂ = (1/n_eval) Σ_{o ∈ eval} d_o`.

**Scope of the quantity.** `ΔL` is **algorithm-specific predictive
improvement**. It is **not** model-free information value. It depends on
the family, the asset, the age, the target definition, the algorithm `𝒜`,
the loss ℓ and the population `𝒫`. A different algorithm could produce a
larger, smaller or opposite-signed value. **No general bound** relates `ΔL`
to an optimal or model-free value of information.

**Pairing.** Both predictions are evaluated on **identical releases and
identical target realizations**. Pairing removes between-release
differences in difficulty from the comparison, but it does **not** remove
target-related uncertainty. Under squared error, for example:

`d_o = (ŷ₁ − ŷ₀)(2Y_o − ŷ₀ − ŷ₁)`.

The difference therefore still scales with the realized target. A few large
target realizations can dominate `ΔL̂`, and `ΔL̂` carries sampling error
from the finite set of evaluated clusters (§K).

### F.2 Losses

| Role | Loss | ℓ(y, ŷ) | Units of ΔL | Interpretation |
|---|---|---|---|---|
| **Primary endpoint** | Raw-return **MSE** | `(y − ŷ)²` | Squared simple return | Matches the ridge squared-error objective, so it evaluates each procedure on the criterion it targets: the conditional mean. Sensitive to extreme returns. |
| **Secondary endpoint (descriptive sensitivity)** | Raw-return **MAE** | `\|y − ŷ\|` | Simple return | Evaluated on **exactly the same predictions** `ŷ₀` and `ŷ₁`. No separate MAE-optimized model is fitted in the primary experiment. Less sensitive to extreme releases. Because the predictors target the mean, not the median, MAE is a robustness description, not an MAE-optimal comparison. |

- The two differences are in **different units** and answer different
  questions, so they are never combined or compared numerically.
- An optional descriptive relative MSE change, `ΔL_MSE / E[ℓ(Y, ŷ₀)]`, may
  be reported. It is not an endpoint.
- Target scaling (for example `Y / U`) is **not** used in the primary
  endpoint (§N2).

### F.3 Conditional information and its relation to ΔL

**Quantity.** `I(S; Y | Z)` is a **model-free** functional of the joint
distribution. It is zero if and only if Y is conditionally independent of S
given Z, and it captures any form of conditional dependence.

**How it differs from ΔL.**

| Aspect | `I(S; Y \| Z)` | `ΔL_{…,𝒜,MSE,𝒫}` |
|---|---|---|
| Depends on | The joint distribution and the choice of Z | Additionally on the algorithm 𝒜, the finite training histories, the loss and the evaluation period |
| Sign | ≥ 0 | Any sign. Estimation noise can make it negative even when `I(S; Y \| Z) > 0`. |
| Captures | All conditional dependence | Only what the algorithm converts into lower loss under ℓ |
| Units | Bits | Squared return (MSE) |

**Orientation only.** If `(Y, Z, S)` were jointly Gaussian and both
predictors were the **population** linear projections, then
`I(S; Y | Z) = −½ log₂(MSE₁/MSE₀) = −½ log₂(1 − ρ²_{YS·Z})`. This identity
does **not** hold for estimated predictors, heavy tails or nonlinearity, and
it is not used as an estimator.

**The conditioning set.** Conditional MI is **not monotonic** in the
conditioning set. Adding a variable to Z can increase or decrease
`I(S; Y | Z)`:

- it can decrease when the added variable carries the same information as
  S;
- it can increase when the added variable reveals dependence that is masked
  marginally, through synergy or explaining-away effects.

No direction can be assumed when Z changes.

**Further limitations.**

- Estimating CMI with multivariate Z at small n is strongly biased and
  variable.
- Valid conditional-independence inference requires
  conditional-randomization-type nulls and its own calibration. Plain
  permutation of S is not valid.
- No CMI estimator is selected or implemented here.

**Not profit.** Neither `I(S; Y | Z)` nor `ΔL` is trading profit or
economic value (H3, §I.3).

**Initial endpoint.** `ΔL_MSE` is proposed as the initial endpoint because
it is:

- operationally defined;
- out-of-sample by construction;
- tied to a fully specified simple algorithm;
- free of any CMI estimator calibration.

CMI remains a candidate for later work.

---

## G. Candidate prediction models and fair comparison

| Model | Role |
|---|---|
| **G1. Constant historical baseline:** `ŷ` = training-history mean of `Y` | Reference only. A descriptive check of whether F0 beats a constant. It is not part of the H1 contrast. |
| **G2-F0. Ridge regression on market-only `Z`** | The F0 procedure |
| **G2-F1. The same ridge procedure on `(Z, S)`** | The F1 procedure |
| **G3. Small nonlinear model** | Future sensitivity only (category C); not in the initial experiment. No neural networks. |

**Fairness requirements.** These apply to G2-F0 and G2-F1 at every
walk-forward step.

1. **Identical eligible releases.** Both procedures use the same
   common-support cohort and the same evaluation observations.
2. **Identical training and evaluation dates.** For each evaluation cluster
   both use the same training history `𝒟(c)`, and are refit at the same
   walk-forward steps.
3. **Identical `Z` features.** F1 differs from F0 only by the added column
   `S`.
4. **Training-only preprocessing.**
   - Standardization means and SDs are fitted on the training history
     only.
   - Within inner validation, scalers are fitted on **the training portion
     of each inner fold only**.
   - `S` is re-standardized within training exactly like the `Z` columns.
5. **Symmetric, separately performed tuning.** λ is chosen separately for
   F0 and for F1, by the same procedure:
   - the **same λ candidate grid** for F0 and F1; its exact values are frozen
     before synthetic calibration is executed (§N7);
   - the **same inner validation folds**: forward-chaining chronological
     folds over whole clusters within `𝒟(c)`;
   - the **same objective**: mean inner-validation squared error;
   - the **same tie-breaking rule**: the largest λ among exact ties;
   - the **same search budget**: the full grid, every time.
6. **Identical fit-failure handling (abort rule).**
   - Predefined **data-eligibility exclusions** (§D.6–D.7) are applied
     **before** any model fitting, identically for F0 and F1.
   - After that, any **unexpected** F0 or F1 fitting or prediction failure
     **aborts the analysis run**. Examples are a non-finite coefficient, a
     non-finite prediction, a singular system or a numerical error.
   - **No pair is silently removed,** and the evaluation population is
     never changed in response to model outcomes.
   - On an abort, the run records the failing procedure, the cluster and
     the numerical diagnostics, together with the reason for aborting.
     Diagnostics include condition numbers, the selected λ and any
     non-finite values.
   - Any alternative failure policy requires a protocol amendment before
     real-data analysis.
7. **S penalized like other features.** `S` enters the same L2 penalty as
   the standardized `Z` columns.
8. **Intercept.** The intercept is unpenalized and fitted on the training
   history. With centred features it equals the training mean of `Y`.

**Effect of penalizing S.** The penalty shrinks the S coefficient toward
zero. Its effect on out-of-sample `ΔL` has **no general sign**:

- shrinkage can lower variance and *improve* F1's out-of-sample loss;
- it can also attenuate a real effect.

Penalizing S does **not** necessarily bias `ΔL` toward zero. Symmetric
penalization is chosen so that F1 differs from F0 only by its inputs.

**Freeze timing (§N7).**

**Frozen before implementation** (structure):

- symmetric model comparison;
- shared training and evaluation observations;
- inner chronological validation;
- separate but symmetric tuning;
- the same tuning budget;
- identical failure handling (abort rule);
- S penalized like the other features;
- an unpenalized intercept.

**Frozen before synthetic calibration is executed** (numerical settings):

- the exact λ grid;
- the minimum training size;
- the inner fold count;
- the retuning schedule;
- numerical tolerances, including the definition of a failure.

No real-data outcome may influence any of these choices.

**Sample size.**

- One panel observation contributes at most one row per cluster.
- The retrospective evaluation period (2023-01 to 2026-09) contains **at
  most about 45 monthly releases per family**, before the common-support
  and window-validity exclusions.
- Early walk-forward steps train on fewer clusters than later ones.
- M4 cohort sizes are **unknown**, because M4's window and feature
  requirements differ from M3's. **M3 cohort sizes are not M4 cohort
  sizes.**
- The feature dimension is fixed at three regressors for F0 and four for
  F1, plus intercepts. CPI and Core CPI, and SPY and QQQ, do not add
  independent clusters.

---

## H. Initial age/horizon design

### H.1 Fixed grid

| Age a | Decision time (ET) | Last feature-eligible bar | Target bars | Target interval (ET) |
|---|---|---|---|---|
| 1 | 08:31 | `b(0)` | `b(1)` … `b(5)` | 08:31–08:36 |
| 5 | 08:35 | `b(4)` | `b(5)` … `b(9)` | 08:35–08:40 |
| 15 | 08:45 | `b(14)` | `b(15)` … `b(19)` | 08:45–08:50 |
| 30 | 09:00 | `b(29)` | `b(30)` … `b(34)` | 09:00–09:05 |

- Each target window is disjoint from its own information set.
- **Across ages,** the a = 1 and a = 5 targets share bar `b(5)`, and earlier
  targets fall inside later information sets. Evaluation differences at
  different ages of the same cluster are therefore **dependent**.
- The grid is fixed in this document. It is not selected, pruned or
  extended in light of M3 or M4 outcomes.
- The statistical design uses no execution lag (g = 0). Lags of one minute
  or more are reserved for H3.

### H.2 Evaluation scheme

- **Walk-forward.** An expanding window over clusters in chronological
  order, starting from the first eligible 2016 cluster. It refits at every
  evaluation cluster, subject to a frozen minimum number of training
  clusters (§N5).
- **Retrospective evaluation population.** Clusters dated 2023-01-01 to
  2026-09-30 (§F.1). Earlier test-period clusters enter later training
  histories as the window expands.

### H.3 What "age" can and cannot identify

Decision age is **confounded** with:

- **clock time,** i.e. 08:31 versus 09:00 ET;
- **liquidity,** which changes across the pre-market and approaching the
  open;
- **evolving market features:** `M(a)` covers more post-release minutes
  at larger a, while `Q` and `U` do not change with age;
- **available price history:** larger a gives the predictor more
  post-release bars;
- **the target-window distribution:** volatility and return distributions
  of 08:31–08:36 and 09:00–09:05 differ for reasons unrelated to the
  release;
- **market-state changes** over the post-release interval.

The initial design can therefore estimate **age-indexed algorithmic
predictive improvement** `ΔL(a)`. It **cannot identify a pure causal
information-aging effect.** A difference across ages, if found, would not
isolate the aging of information from the other changes listed above.

---

## I. Hypotheses

All hypotheses are open. None is claimed to be supported.

### I.1 H1: incremental predictive improvement

There are **12 SPY primary hypotheses**, one for each family
f ∈ {CPI_MOM, CORE_CPI_MOM, NFP} and age a ∈ {1, 5, 15, 30}:

- `H1₀^{f,a}: ΔL_{f, SPY, 𝒜, MSE, 𝒫}(a) ≤ 0`
- `H1_A^{f,a}: ΔL_{f, SPY, 𝒜, MSE, 𝒫}(a) > 0`

**H1 is the only confirmatory hypothesis family in the initial M4
experiment.** Its primary endpoint is the raw-return MSE loss improvement.

**Multiplicity.** One family of the 12 SPY tests. The correction is
**Holm** across all 12, at FWER α = 0.05. Holm is valid under arbitrary dependence, and
the tests are dependent through shared clusters (CPI and Core CPI) and
shared clusters across ages.

**Secondary analyses** (not confirmatory, and not in the Holm family):

- QQQ: the same 12 hypotheses, reported as a separate secondary
  replication family;
- MAE differences on the same predictions, reported as descriptive
  sensitivity;
- G1 versus F0, reported as a descriptive check.

**Interpretation.** Rejecting `H1₀^{f,a}` supports algorithm-specific
predictive improvement for that panel and population. It does not show
model-free information, freshness or economic value. Failing to reject is
not evidence that S carries no information (§E).

### I.2 H2: age variation of algorithm-specific improvement (planned secondary question; not tested initially)

The question is whether algorithm-specific predictive benefit varies across
decision ages. For each SPY family f, the proposed null is:

- `H2₀^f: ΔL_f(1) = ΔL_f(5) = ΔL_f(15) = ΔL_f(30)` (MSE, the same 𝒜 and
  𝒫)
- `H2_A^f`: not all four are equal.

**Status.** H2 is a **planned secondary research question** on the M4
roadmap. It is **not tested in the initial M4 experiment**. That
experiment reports **no H2 p-values, no H2 confidence intervals and no H2
statistical conclusions**. Any descriptive display of `ΔL̂(a)` side by side
across ages carries no age-comparison inference.

**Required before any future H2 test.** All of the following, in a
committed protocol amendment:

1. select the test statistic, for example a contrast on paired
   cluster-level vectors `(d_c(1), d_c(5), d_c(15), d_c(30))`;
2. specify dependence-aware inference covering overlapping ages and
   shared clusters;
3. define the multiplicity rule and the α allocation relative to H1;
4. calibrate the full H2 procedure synthetically;
5. freeze the protocol.

Rejecting `H2₀` would describe age-indexed, algorithm-specific
differences under the confounds of §H.3. It would **not** be information
decay. **No directional (decay) hypothesis or claim is part of M4's initial
design.**

### I.3 H3: economic value (future research phase)

Let `D` be a decision rule fixed in advance. For example: at age a, after
an execution lag g ≥ 1 minute, take a fixed-size position in the direction
of the F1 forecast, and close it at the end of the target window.
Let `κ` be a fixed cost model covering spread, slippage, fees and pre-market
liquidity. Define

`EV_{D,κ}(a) = E_𝒫[ net payoff of D using F1 forecasts ] − E_𝒫[ net payoff of D using F0 forecasts ]`,

with both terms net of the same cost model κ.

- `H3₀: EV ≤ 0`
- `H3_A: EV > 0`

H3 belongs to a **later research phase** with its own design freeze, cost
data and calibration. **No trading profitability is evaluated in the
initial M4 experiment.**

---

## J. Leakage and point-in-time requirements

1. **Strict chronology by cluster.**
   - Every fitted quantity for evaluation cluster c uses only clusters
     strictly earlier than c. This covers coefficients, λ, scalers and
     baseline means.
   - No random train/test split, no shuffled k-fold, and no leave-one-out
     across time.
   - Inner validation folds are forward-chaining over whole clusters.
2. **Completed targets only.** A training sample is eligible only if its
   target window completed before the training cutoff (§E).
3. **No future-informed transformations.**
   - Scalers and winsorization thresholds (if any) use
     only data before the relevant cutoff.
   - Within inner folds, they use only the fold's training portion.
   - No full-sample z-scoring.
4. **No cross-release leakage.** Every panel observation of one economic
   release cluster is on the same side of every split (§D.8). Neither
   CPI-family panel trains on the other's observation from the same or a
   later cluster.
5. **Release grouping in inference.** Resampling and randomization operate
   on clusters or blocks of clusters, never on individual panel
   observations (§K.3).
6. **Bar timing.**
   - Feature eligibility requires scheduled bar completion.
   - Zero delivery latency is an idealized assumption.
   - Targets require the full five-minute window under §D.6. There is no
     shortening, filling or substitution.
7. **Timestamp and vintage limits.**
   - Scheduled minute-precision release times are not actual publication
     times (A-pub).
   - The forecasts come from retrospectively saved pages and are not
     point-in-time verified. Only a small number of actuals were
     cross-checked against official vintages (M3 report §3).
   - `S` is the surprise **as recorded**, which may differ from the
     surprise observed in real time.
8. **No hindsight selection.** Ages, Δ, families, features, the λ grid,
   folds, the losses, the endpoint, the inference candidates, the selection
   criteria and the multiplicity rules are all frozen before any real-data
   M4 computation. Class-A decisions and the N8 candidates and criteria are
   frozen before implementation or calibration. The selected N8 procedure is
   frozen after calibration and before real data (§N).

---

## K. Statistical evaluation considerations

### K.1 Three kinds of validation

| Kind | Definition | Status in M4 |
|---|---|---|
| **A. Retrospective model out-of-sample evaluation** | Walk-forward predictions on historical clusters, each predicted from strictly earlier clusters | Planned (first experiment). It is out-of-sample **with respect to model fitting** only. |
| **B. Scientific holdout** | Data that **no** prior analysis has examined, reserved before any analysis | **Not available historically.** M3 has already examined the whole 2016–2026-09 period, so the retrospective M4 evaluation is **not** an untouched scientific holdout. |
| **C. Prospective validation** | Evaluation on releases whose data are captured **after** the design freeze, under a frozen capture protocol | Future (§N6) |

**When prospective validation begins.** Only after all three of the
following:

1. the M4 design freeze, i.e. the committed M4 methodology;
2. the freeze of a prospective data-capture protocol;
3. the actual start of capture.

Releases after 2026-09-30 are **not automatically prospective**. A release
counts as prospective only if its data were captured under the frozen
protocol, after the start of capture.

**The prospective capture protocol must record, per release:**

- a **forecast snapshot taken before the release**, with its acquisition
  timestamp;
- the **initial published actual value**, with its acquisition timestamp;
- the **actual publication timestamp**, when it is available from the
  publisher;
- **market-bar arrival timestamps**, i.e. when each bar was received;
- the **bar correction history**, i.e. later revisions of received bars.

None of these data exist in the repository today. The protocol is a future
design item.

### K.2 Test statistic for H1

The per-hypothesis statistic is `ΔL̂_MSE(f, a)` and its studentized form.

**Clark–West and similar nested-model adjustments** test a *different* null,
namely population-level predictive content of S. They do **not** test the
algorithm-specific `ΔL ≤ 0`, and **are not admissible as direct evidence of
raw algorithmic loss improvement.** They may be reported only as clearly
labelled secondary diagnostics.

### K.3 Candidate inference procedures (to be compared in calibration; none selected yet)

| Candidate | Description | Key assumptions | Calibration needs |
|---|---|---|---|
| **P1. Cluster-level studentized mean test** | Aggregate `d_o` to cluster level, then a one-sided test using a HAC (Newey–West-type) variance over the chronological cluster series | Weak temporal dependence, finite variance, adequate n. Heavy tails threaten its accuracy at about 45 clusters. | Size under heavy tails and heteroskedasticity, with partial nulls |
| **P2. Studentized moving-block bootstrap over clusters** | Resample contiguous blocks of chronologically ordered clusters, keeping all ages, families and symbols of a cluster together | Approximate stationarity within the period; the block length (a frozen rule) | Size and power across block lengths and regime-shift scenarios; one-sided CI coverage |
| **P3. Cluster sign-flip randomization** | Randomly flip the sign of the cluster-level `d_c` | Symmetry of `d_c` about 0 under the null. This is **not implied** by the composite null `ΔL ≤ 0`, so validity is restricted. | Size under asymmetric heavy-tailed `d` and under strictly negative `ΔL` |

**Pre-specified selection criteria**, applied only to P1–P3 in synthetic
calibration:

1. **Size control.** Across every global and partial null scenario, the
   one-sided rejection rate must not exceed α by more than a frozen
   tolerance. The tolerance is assessed with a one-sided Wilson upper bound,
   as in M3 Amendment 03.
2. Among the candidates that pass (1), the **highest power** at the frozen
   minimal effect sizes.
3. On ties, the **simplest procedure** in the order P1, P2, P3.

If no candidate passes (1), the stop/go gate fails (§K.6). **New candidates
cannot be added after calibration results are seen** without a documented
amendment and a full recalibration of the whole candidate set.

### K.4 Multiplicity

- **Initial experiment (confirmatory).** H1 only: 12 SPY family × age
  hypotheses, Holm at FWER α = 0.05.
- **Secondary.** QQQ is a separate replication family, reported alongside
  but not included in the confirmatory Holm family. MAE and G1 versus F0
  are descriptive.
- **H2.** Not tested initially (§I.2).
- **Superseded rule.** The revision-2 gatekeeping proposal (H2 gated on H1
  rejections, Holm over three families, combined bound α₁ + α₂) is
  **withdrawn**. Any future H2 multiplicity and α allocation will be defined
  in the H2 protocol amendment.

### K.5 Synthetic calibration requirements (not performed in this task)

Before any real-data predictive evaluation, a synthetic calibration study
must exercise:

- the **entire walk-forward pipeline**: cluster ordering, expanding
  training, inner folds, refits and minimum training size;
- **preprocessing**: training-only scaling and inner-fold scaling;
- **ridge tuning**: the grid, folds, objective, tie-breaking and symmetric
  F0/F1 tuning, together with the abort rule. The calibration records any
  abort and its diagnostics; a configuration that aborts in calibration
  must be amended before real data.
- **paired loss differences**, under MSE and also under MAE for reporting;
- **heavy-tailed returns** and **heteroskedasticity**: volatility varying
  across releases and ages;
- **temporal regime variation**: shifts in the S–Y relation and in
  volatility across 2016–2026;
- **overlapping decision ages**: shared bars and earlier targets inside
  later features;
- **economic-release dependence**: shared CPI and Core CPI clusters and
  correlated families;
- **global and partial null cases.** The global null has no signal at any
  age. Partial nulls have signal at some ages or families only. A further
  null class is described below.
- **H1 multiplicity**: Holm FWER over the 12 tests under each global and
  partial null configuration. H2 calibration is not part of this study
  (§I.2);
- **confidence-interval coverage** for `ΔL`;
- **detectable effect sizes**: power curves at the realistic cluster counts
  (at most about 45 evaluation clusters per family).

**A zero conditional signal is not a zero ΔL.** These must be separate
calibration scenarios:

1. **No conditional signal:** `Y ⟂ S | Z`. The F1 procedure typically has
   `ΔL < 0`, because the extra parameter adds estimation noise.
2. **Weak true signal with ΔL ≤ 0:** S carries information, but the ridge
   procedure cannot convert it into lower loss at the available n. This is
   a member of H1₀ that the test must not reject at more than α.
3. **Signal with ΔL > 0:** used for power.

Because H1₀ is composite and the boundary `ΔL = 0` may be reached with a
true signal present, size must be checked at that boundary, not only under
`Y ⟂ S | Z`.

### K.6 Stop/go gate before real-data predictive evaluation

The gate is fixed in advance and must be frozen before calibration results
are seen. Real-data predictive evaluation proceeds **only if** all of the
following hold:

1. at least one H1 inference candidate passes the size-control criterion in
   every frozen null scenario;
2. the H1 Holm procedure controls FWER at 0.05 in calibration under every
   frozen null configuration;
3. nominal-95% interval coverage for `ΔL` is within a frozen tolerance;
4. the pipeline passes deterministic reproducibility checks: identical
   outputs on rerun, with fingerprinted inputs and code;
5. the minimal detectable `ΔL` at the realistic cluster count is reported,
   so that a non-rejection can be interpreted;
6. no abort occurred in any calibration configuration of the frozen
   numerical settings.

**H2 calibration is not required** for this initial H1-only evaluation. It
belongs to the future H2 protocol (§I.2).

Power is reported but is not a gate. If the gate fails, the design is
amended, committed and **recalibrated in full** before any real data is
touched. Calibration results never license an unregistered real-data
method.

---

## L. Limitations

| Area | Limitation |
|---|---|
| Effective sample | At most about 45 retrospective evaluation releases per family, before exclusions; low power for small `ΔL` |
| Cluster dependence | CPI and Core CPI share clusters and targets. SPY and QQQ share clusters. Ages share clusters and bars. None of these are independent replications. |
| Reaction already in prices | F0 contains the post-release path. A non-positive `ΔL` cannot separate "already reflected", "never predictive" and "not exploitable by 𝒜". |
| Algorithm-specificity | `ΔL` holds for 𝒜, ℓ and 𝒫 only. It is not a model-free or optimal value of information, and no bound to such a value is claimed. |
| Age confounding | Age is confounded with clock time, liquidity, features, history length, target-window distribution and market state (§H.3). |
| Forecast vintages | The surprise is the recorded surprise, not point-in-time verified. |
| Timestamps and latency | Scheduled, not actual, publication times; idealized zero delivery latency; historical bar vintages |
| Pre-market liquidity and coverage | All windows are pre-market. Exclusions for missing bars are non-random and correlated with liquidity, era and symbol. |
| Regime shifts | Expanding training mixes monetary and volatility regimes; the estimand is period-specific. |
| Multiple testing | 12 confirmatory SPY H1 tests (Holm). H2 is not tested. Secondary analyses (QQQ, MAE, G1) are not confirmatory. |
| No untouched holdout | M3 has examined the historical period. Only prospective validation under a frozen capture protocol is unseen data. |
| Economic value | Outside the initial experiment; no trading conclusions |
| Causality | None. All quantities are predictive or statistical. |

---

## M. Temporal Dependence Analysis backlog (P2)

**M4-P2: Temporal Dependence Analysis.**

| Field | Value |
|---|---|
| ID | M4-P2 |
| Priority | **P2** |
| Status | Backlog. **Outside the initial M4 experiment.** Not implemented and not run. |

**Scope:**

- observation-window length: Δ and the lengths of the feature windows;
- time since the macro release: the decision age;
- observation lag: the gap between the information window and the target
  window;
- Pearson and Spearman correlation;
- autocorrelation of 1-minute returns around releases;
- cross-correlation: SPY–QQQ, and returns–surprise at multiple lags;
- mutual information: unconditional, and possibly conditional, as a
  descriptive complement;
- overlapping versus non-overlapping return windows;
- release-level dependence: across clusters, and regime clustering;
- market-state effects: volatility regime, pre-release drift and similar.

**Why overlapping cumulative returns create mechanical correlation.** This
uses notation distinct from the frozen M3 simple return `R_h`. Let
`x(k) = ln(C(k)/C(k−1))` be one-minute log returns after the release, and
`X_h = Σ_{k=0}^{h−1} x(k)` the cumulative log return over h minutes. For
`h < h'`:

`X_{h'} = X_h + Σ_{k=h}^{h'−1} x(k)`.

So `X_h` and `X_{h'}` share every term of `X_h`. If the `x(k)` were
independent with equal variance and carried no information at all, then

`Corr(X_h, X_{h'}) = √(h / h')`,

which is purely mechanical. Consequences:

1. **Horizon statistics are not independent.** Dependence statistics
   computed on cumulative windows at different horizons are correlated by
   construction.
2. **A falling cumulative profile can arise from dilution alone.** Suppose
   S affected only the first minute. Its association with `X_h` would still
   fall as h grows, because the added minutes contribute variance unrelated
   to S. A declining cumulative-dependence profile is therefore not, by
   itself, evidence that information is lost or absorbed.
3. **Non-overlapping increments ask a different question.** Windows such
   as the M4 targets ask whether S is associated with **new** price
   movement.

M4-P2 would quantify these mechanical relations before any freshness
interpretation.

---

## N. Design decision matrix

**Classification** (decision-level labels from the independent review):

| Class | Meaning | Decisions |
|---|---|---|
| **A** | Must be frozen before implementation | N1, N2, N3, N4, N5, N6, N7, N9, N10, N11, N12, N13, N15 |
| **B** | Calibration-design decision | N8 |
| **C** | Future work | N14 |

Some class-A decisions contain **operational parameters that are
explicitly deferred** to the calibration design. Each is named in the
"Deferred operational parameters" column. Every deferred parameter must be
frozen **before synthetic calibration is executed**, and no real-data
outcome may influence it.

**N8 timing (calibration-design decision).**

- **Before synthetic calibration:** freeze the inference candidates P1–P3,
  their assumptions and the selection criteria (§K.3).
- **After calibration, before any real-data predictive analysis:** freeze
  the selected procedure.
- Calibration evaluates only the frozen candidates against the frozen
  criteria. Changing candidates after seeing calibration results requires
  a committed amendment and a full recalibration. Calibration is not an
  open-ended method search.

| # | Decision | Class | Frozen choice / recommendation | Scientific rationale | Dependencies | Deferred operational parameters (frozen before calibration execution) | Calibration requirements |
|---|---|---|---|---|---|---|---|
| N1 | Primary loss | **A** | Raw-return MSE difference primary. MAE on the same predictions as secondary descriptive sensitivity. | MSE matches the ridge objective and the conditional-mean target. MAE adds robustness without new models. | N7 | None | Size and power of `ΔL_MSE` under heavy tails; how often MAE and MSE disagree |
| N2 | Target | **A** | Raw simple return `Y_i(a)` over the full 5-minute window; no scaling | No second estimated quantity in the target; interpretable units | N1, D.6 | None | Heteroskedastic scenarios with a raw target |
| N3 | Feature set `Z` | **A** | Exactly `Q`, `U`, `M(a)` as defined in §D.5 (31-close pre-release window). No volume, no post-release volatility, no other indicators, no nonlinear transforms. | Minimal dimension for small n; every feature built from feature-eligible, complete bars | D.5, D.6 | None | Collinearity and stability with three regressors |
| N4 | Cohort | **A** | Common support across all four ages per (f, s) | Identical clusters across ages; keeps any future H2 well defined | D.6, D.7 | None | Realistic exclusion patterns in the synthetic cohorts |
| N5 | Training scheme | **A** | Expanding window over clusters from 2016; refit per evaluation cluster, strictly earlier clusters only | Uses all earlier data; deterministic | N6, N7 | Minimum training size; retuning schedule | Effect of small early training sets on size and power |
| N6 | Validation types | **A** | Frozen distinction: retrospective model out-of-sample evaluation (2023-01-01 to 2026-09-30 clusters) ≠ scientific holdout ≠ prospective validation. Frozen obligation: prospective validation is required in the future (§K.1). | M3 examined the historical period, so no untouched holdout exists | §K.1 | None. Building the prospective capture infrastructure is future work, not a parameter. | Power at about 45 clusters per family |
| N7 | Ridge procedure | **A** | Frozen structure: symmetric comparison; shared observations; inner chronological validation; separate but symmetric tuning; same budget; abort-on-failure; S penalized like `Z`; unpenalized intercept (§G) | Fair F0/F1 comparison differing only in inputs | N3, N5 | Exact λ grid; minimum training size; inner fold count; retuning schedule; numerical tolerances and failure definition | Tuning stability; how often λ hits the grid edge; abort diagnostics |
| N8 | H1 inference procedure | **B** | Candidates P1–P3, assumptions and selection criteria frozen **before** calibration; selected procedure frozen **after** calibration and **before** real-data analysis. Clark–West only as a labelled secondary diagnostic. | Cluster dependence and heavy tails rule out assuming iid release resampling | N9, N12 | Block-length rule (P2); HAC bandwidth rule (P1); size tolerance, all frozen with the candidates | Size in global and partial nulls, including the "signal but ΔL ≤ 0" boundary; power; CI coverage |
| N9 | Multiplicity | **A** | H1 only: Holm over the 12 SPY tests at α = 0.05. QQQ as a separate secondary replication family. H2 not tested initially; the revision-2 gatekeeping is withdrawn. | Holm is valid under arbitrary dependence; a single confirmatory family | N8, §I | None for H1. H2 multiplicity belongs to the future H2 protocol. | H1 FWER under all null configurations |
| N10 | Simultaneous macro information | **A** | Excluded from both F0 and F1 | Keeps F1 to F0 a single-variable change | N3 | None | None beyond the base design |
| N11 | Execution lag | **A** | g = 0 for the statistical H1 evaluation; g ≥ 1 only within future H3 | Statistical prediction does not need execution | §I.3 | None | None (H3 is calibrated separately later) |
| N12 | Synthetic calibration | **A** | Required before real-data predictive evaluation. Frozen scenario list (§K.5) and H1-only stop/go gate (§K.6). | Calibration-first discipline, as in M3 | N7, N8, N9 | Data-generating parameter values (tails, heteroskedasticity, regimes, effect sizes) | This item *is* the calibration specification |
| N13 | Delivery latency | **A** | Zero delivery latency, frozen as an **idealized retrospective assumption** (§D.2), together with A-avail (§D.4) | Historical arrival times are unknown | §K.1 | None. Realistic latency modelling is future work. | None for the retrospective experiment |
| N14 | G3 nonlinear model | **C** | Deferred; sensitivity only after primary results are frozen | Small n; overfitting and method-shopping risk | Primary results | — | Its own calibration later |
| N15 | Age hypothesis form | **A** | H2 is frozen as an **equality-based** planned hypothesis (§I.2), not tested initially. No directional information-decay inference in the initial experiment. | Age is confounded (§H.3) | §I.2 | None | None initially; future H2 protocol |

**Proposed sequence.**

1. Final review of this definition.
2. Freeze the class-A decisions and the N8 candidates and criteria in a
   committed M4 methodology document.
3. Implement the pipeline without touching real data.
4. Freeze the deferred operational parameters and the calibration design.
5. Run the synthetic calibration.
6. Evaluate the H1 stop/go gate.
7. Freeze the selected N8 procedure.
8. Run the retrospective H1 real-data evaluation.
9. Later phases: the prospective capture protocol and validation, the H2
   protocol, H3 and M4-P2.
