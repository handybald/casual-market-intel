# M3 Information Decay v1 — Amendment 02: Revised Calibration Criteria and Estimand Hierarchy

| Field | Value |
|---|---|
| Type | **post-synthetic-calibration, pre-real-data methodology revision** |
| REAL MARKET MI COMPUTED | **NO** |
| Amends | `docs/research/information_decay_v1_methodology.md` (frozen at `d976c3e408ec770ffa6633af22b422d3814e0e16`) and `docs/research/information_decay_v1_amendment_01.md` |
| Trigger | Amendment 01 outcome (no estimator qualified) and an independent review of the synthetic calibration |
| Status at commit | **Frozen before any Amendment-02 revalidation result exists.** Revalidation results are appended afterwards in a separate, uncommitted results document. |

Amendment 01 stays as an **audit artifact** and is not edited. Where it is
wrong, §13 below corrects it, and §13 supersedes the affected statements.

All decisions in this amendment rest on **synthetic** evidence and on the
review of that evidence. No real surprise, response or residual value, and no
real MI, has been read or computed. Several choices below change the frozen
protocol after synthetic results were seen: D1, D4, V4, the Monte Carlo
decision rule and D8. They are revisions, labelled as such. They were **not**
part of the original frozen protocol.

---

## 1. Revised estimand hierarchy (supersedes methodology §2–§3 and F1)

### 1.1 Primary

`I(S; R_h)`, the **raw response**, on SPY common-support releases (`cs_raw`):

| Item | Value |
|---|---|
| Families | CPI m/m, Core CPI m/m, NFP |
| Horizons | 1, 5, 15, 30, 60 minutes |

This is the main M3 v1 information profile.

**Reason.** The synthetic calibration (Amendment 01 §3.4) showed two things:

- The sign-conditioned M2 baseline removes most of a surprise-linked linear
  signal. Under raw ρ = 0.35, panel power was 0.22–0.32 for the residual and
  0.90–0.99 for matched raw.
- Valid residual inference needs pipeline-aware recomputation of the
  baseline.

D4 was deliberately left open to calibration evidence, and no real MI has
been inspected. The revision rests on synthetic evidence only.

### 1.2 Secondary: baseline-adjusted residual sensitivity

`I(S; E_h)`, `h ∈ {1, 5, 15, 30, 60}`, on `cs_resid`.

- **Inference uses pipeline-aware Null B only** (methodology §11.7).
- **Fixed-residual Null A is invalid for residual inference.** Its synthetic
  FPR was about 0.10–0.12 under heavy-tailed responses, while Null B stayed
  near nominal. Null A may be computed only as a labelled diagnostic.
- Residual MI is **never** the main information-decay curve.
- It is **never** called conditional mutual information.

### 1.3 Matched raw versus residual comparison (secondary)

Any raw-versus-residual comparison uses the **same** release cohort
(`cs_resid`), horizons, family and symbol.

### 1.4 Replication and sensitivity

- **QQQ** is secondary replication, analysed separately and never pooled.
- **KSG** with k ∈ {3, 5, 10} is a nonlinear sensitivity family (§6).

---

## 2. Scientific naming of GCMI (supersedes methodology §8.1 wording)

The scalar estimator is described as:

> **Permutation-null-adjusted Gaussian-copula dependence, reported in bits,
> derived from marginal normal-score dependence.** It is primarily sensitive
> to monotonic association and is not an exhaustive estimator of arbitrary
> nonlinear dependence.

It is never called "universal nonlinear mutual information". Lower-bound
language applies only to continuous marginals with no ties, where the
Gaussian-copula MI lower-bounds the true MI. It is **not** claimed for tie
policies applied to discretised surprises, or for bootstrap resamples with
duplicates.

---

## 3. Revised validity gates for the primary estimator

| Gate | Type | Definition |
|---|---|---|
| **V1** | hard | Horizon-adjusted (max-T) panel type-I under stratified permutation in `s1_null_modeA`, `s1_null_modeB`, `s6_null_t3` and `s10_regime_null` |
| **V2** | hard | Horizon-adjusted panel type-I in `s9_null_discrete` under the D3-selected tie policy |
| **V3** | hard | Unchanged point rule (§3.1), on a revised cell list |
| **V4a** | hard | Contaminated-null validity (§3.2) |
| **V4b** | sensitivity, **not** a gate | Contaminated-alternative behaviour (§3.3) |

The old V4 is **withdrawn**. It compared estimates under *dependent*
contamination to the *clean* MI. That contamination changes the population
joint distribution and therefore the true MI, so the distance from clean MI is
not a valid robustness gate.

### 3.1 V3 (revised cell list; rule unchanged)

- **Rule:** `mean MI_eff ≤ I_true + 2·SE(MI_eff) + 0.10·I_true`, where the SE
  is clustered by replicate.
- **Cells:** `s2_rho015`, `s2_rho025`, `s2_rho035_modeA`, `s2_rho050`,
  `s3a_copula_monotone`, `s3b_monotone_tanh`, `s6_alt_t3`.
- **Sample sizes:** grid n ∈ {60, 80, 100, 120}, plus n ∈ {77, 97} where
  stored (`s2_rho035_modeA`).
- `s7_alt_contaminated` is **removed** from V3 and reclassified as V4b.
- V3 is a point rule on the mean, not a binomial cell, so it is not part of
  the Monte Carlo classification family of §4.

### 3.2 V4a — contaminated-null validity (hard gate)

New cell: `s7v4a_null_independent_contamination`, mode A. The construction
below is frozen.

For replicate `r` of size `n`, every draw comes in this order from the
replicate's data generator
`rng_for("calibration", "s7v4a_null_independent_contamination", n, r, "data")`:

1. `S_i ~ N(0, 1)` iid for `i = 1..n`.
2. `R = Z`, the cumulative 5-horizon noise of methodology §9.1. `R` is
   independent of `S`.
3. `c = max(1, round(0.02·n))`. Python rounding applies; none of the n used
   here has a .5 case. That gives c = 1 at n = 60, and c = 2 at n = 77, 80,
   97, 100 and 120.
4. `M_S`: `c` indices drawn without replacement from {1..n}. `U_S`: `c`
   independent fair signs ±1.
5. `M_R`: `c` indices drawn without replacement from {1..n}, independently of
   `M_S`. They may overlap it by chance. `U_R`: `c` independent fair signs
   ±1, independent of `U_S`.
6. Set `S[M_S] = 4·U_S`. Set `R[M_R, h] = −10·U_R` for **every** horizon `h`,
   which keeps the contaminated response vector consistent across correlated
   horizons, at the scenario-7 magnitude.

The S-side draws (1, 4) and the R-side draws (2, 5) are mutually independent,
so the vector `S` is independent of the matrix `R`. Population independence
holds by construction and the stratified permutation null is exact. V4a
therefore asks whether the estimator plus inference stays calibrated in the
presence of outliers that carry no dependence.

- **Magnitude** (4 and −10) and **fraction** (0.02) are copied from scenario 7
  and are **not** tuned.
- **Sizes:** n ∈ {60, 77, 80, 97, 100, 120}.
- **Classification:** §4.
- **KSG** k ∈ {3, 5, 10} is evaluated on the first 1,000 replicates of each
  V4a cell. This is descriptive only and is not classified.

### 3.3 V4b — contaminated-alternative sensitivity (reported, not a gate)

The existing `s7_alt_contaminated` (ρ = 0.35) and `s7_null_contaminated`
(ρ = 0) are reclassified as V4b. Their construction is
`S[M] = 4u`, `R_h[M] = −10u` with a **shared** `u`. The contaminants
therefore **encode dependence themselves**.

**Contaminated reference MI.** Use the population mixture with contamination
probability `p = c/n`. The contamination indicator is almost surely a function
of `S` (atoms at ±4) and of `R_h` (atoms at ∓10), and within the contaminated
component `R_h = −10·sign(S)` carries exactly 1 bit. So:

`I_cont = H_b(p) + (1 − p)·I_clean + p·1` bits.

This is exact for the i.i.d. mixture. It is a reference for the fixed-count
finite-sample construction.

| n | c | p | `I_cont`, alt (ρ = 0.35) | `I_cont`, null (ρ = 0) |
|---|---|---|---|---|
| 40 | 1 | 0.0250 | 0.285569 | 0.193661 |
| 60 | 1 | 0.0167 | 0.231652 | 0.138958 |
| 77 | 2 | 0.0260 | 0.291571 | 0.199755 |
| 80 | 2 | 0.0250 | 0.285569 | 0.193661 |
| 97 | 2 | 0.0206 | 0.257839 | 0.165518 |
| 100 | 2 | 0.0200 | **0.253820** | 0.161441 |
| 120 | 2 | 0.0167 | 0.231652 | 0.138958 |

The clean MI is `I_clean` = 0.094264 bits (alt) and 0 (null). The n = 100
value matches the independent review (0.253820).

**V4b reports**, per estimator and n:

- clean MI and contaminated reference MI;
- mean estimate (`MI_obs` and `MI_eff`);
- error relative to each target;
- median paired influence |contaminated − clean|;
- the change in rejection rate against the clean cell.

An estimate above the clean target is **not** called overestimation, because
the contaminated truth is larger. GCMI's target is the Gaussian-copula
dependence, not `I_cont`, and the V4b error relative to `I_cont` is read with
that in mind.

---

## 4. Monte Carlo decision rule: PASS / INCONCLUSIVE / FAIL (new)

**Ceilings are unchanged.** Every hard type-I cell keeps its frozen point
ceiling:

`c = 0.05 + 2·√(0.05·0.95 / R₀)`, with `R₀ = 2,000`, so `c = 0.0597468`.

The ceiling stays fixed when replicates are added.

**Hard-gate family 𝓗 (42 cells, fixed now).** Every cell is evaluated with
GCMI at n ∈ {60, 77, 80, 97, 100, 120}. The sizes 77 and 97 are the SPY
primary cohort sizes recorded in methodology §7.2.

| Gate | Cells | Count |
|---|---|---|
| V1 | `s1_null_modeA`, `s1_null_modeB`, `s6_null_t3`, `s10_regime_null` | 24 |
| D3 / V2 | `s9_null_discrete` under tie policy A and tie policy B | 12 |
| V4a | `s7v4a_null_independent_contamination` | 6 |

Qualification uses 36 of these cells: V1, V4a and the selected policy's six
V2 cells. The other policy's cells are classified for D3 only.

**Statistic.** In each cell, `p̂ = k/R` is the fraction of replicates whose
max-T panel p-value is ≤ 0.05. The test is the one-sided **Wilson score
test** against the ceiling:

`z = (p̂ − c) / √(c(1 − c)/R)`

- PASS hypothesis `H_P: FPR ≥ c`, rejected for small `z`, with
  `p_P = Φ(z)`.
- FAIL hypothesis `H_F: FPR ≤ c`, with `p_F = 1 − Φ(z)`.

This is exactly the inversion of the Wilson interval.

**Simultaneous procedure: Holm–Wilson, with α split over looks.** This was
chosen by the project owner before any rerun.

- There are at most five looks ℓ = 1..5, at cumulative R = 2,000·ℓ per
  undecided cell.
- The family error budget is α = 0.05, split by Bonferroni across the five
  possible looks: **α_look = 0.01**.
- At each look, among the cells still undecided:
  - **PASS:** `H_P` is rejected by Holm step-down at α_look.
  - **FAIL:** otherwise, `H_F` is rejected by Holm step-down at α_look.
  - **INCONCLUSIVE:** everything else.
- PASS and FAIL are **locked** and get no further replicates.
- INCONCLUSIVE cells get one more batch of **2,000 independent replicates**.
  Batch b uses replicate indices [2000(b−1), 2000b) with the frozen seed
  scheme. Threshold, scenario, contamination, estimator and tie policy never
  change.
- After look 5 (10,000 replicates), a cell still INCONCLUSIVE is final, and
  the estimator/policy it tests does **not** qualify.

This controls, at 0.05, the probability of any false PASS (a cell with
FPR ≥ c classified PASS) across all looks and cells. It is the same for
false FAIL.

**Pre-run operating characteristic.** This is binomial arithmetic only, with
no MI, from 600 simulated rule runs. It is reported so the rule's
conservatism is visible before results exist.

| True FPR in every cell | P(all 36 qualification cells PASS) |
|---|---|
| 0.040 | ≈ 1.00 |
| 0.045 | ≈ 1.00 |
| **0.050**, exactly nominal | **≈ 0.22** |

A cell exactly at the ceiling is falsely passed with probability ≈ 0.007.

A permutation test whose true FPR equals the nominal 0.05 has therefore only
about a 22% chance of qualifying under this rule. The project owner chose
this strictness knowingly. **A STOP under this rule is not, by itself,
evidence of miscalibration.**

**First look.** The first 2,000 replicates of the existing cells reuse the
Amendment-01 seeds. They are recomputed with the corrected code (§11), and
the point rates must reproduce the stored Amendment-01 values wherever the
fixes do not change the computation. Any mismatch is reported.

---

## 5. D1 — primary estimator (supersedes methodology §10.3 D1)

GCMI is the **only** primary candidate. It becomes the v1 primary estimator
only if:

- V1, V2 (with the selected tie policy) and V4a are PASS under §4;
- V3 passes;
- the implementation fixes of §11 are in place with their tests passing.

If any hard gate FAILS, or remains INCONCLUSIVE after 10,000 replicates:
**STOP. No other estimator is promoted.**

---

## 6. KSG role (supersedes methodology §10 candidacy)

KSG with k ∈ {3, 5, 10} is **not** a primary candidate in v1. It is a
nonlinear sensitivity family. The synthetic reasons:

- weaker finite-n power for monotone dependence (Amendment 01 §3.3);
- poor recovery under non-Gaussian marginals (§3.2);
- tie and atom sensitivity;
- no calibrated interval (§3.6).

Its ability to detect U-shaped and heteroskedastic dependence is
scientifically valuable. KSG results are always reported, including
inconvenient ones. **No primary claim may rest on KSG significance alone.**

---

## 7. D2 — minimum n

`n_min = 60` is retained as a **necessary** threshold for the primary
Gaussian-monotonic reference effect. Amendment-01 stored evidence: GCMI
horizon-adjusted panel power 0.902 / 0.890 (modes A / B) at n = 60, with
`MI_eff` bias inside 20% of `I_ref`.

It is **not sufficient**. A panel also needs:

- V1, V2 and V4a PASS at its exact cohort size (77, 97);
- applicable tie-policy calibration;
- valid cohort construction;
- no methodological exclusion.

n_min is **not** lowered to preserve QQQ. QQQ CPI and Core CPI (n = 56) stay
exploratory. QQQ NFP (n = 74) is secondary replication, and its hard gates
are not revalidated at n = 74 (a stated caveat).

---

## 8. D3 — tie policy (reopened; supersedes Amendment 01's D3 = B)

The candidates are **A** (average ranks of the frozen `surprise_std`) and
**B** (average ranks within exact `surprise_raw` tie groups). Policy C is
dropped. The evidence is the 12 `s9_null_discrete` cells of 𝓗.

A policy **passes** if all six of its cells are PASS. It **fails** if any
cell is FAIL. Otherwise it is inconclusive. The §4 batches resolve
inconclusive cells.

Hierarchy:

1. hard type-I calibration;
2. bias and stability (reported);
3. preservation of the frozen standardized-surprise representation;
4. power, only after validity.

Rules:

- If **both** policies pass, choose **A**. It preserves the frozen
  `surprise_std` with the least added transformation.
- If **exactly one** passes, choose that one.
- If neither passes (fail or final inconclusive), **STOP**.

Real MI and power never enter this choice.

**Propagation.** The selected policy is applied by one central function to:

- the raw permutation statistic;
- the residual statistic;
- Null A diagnostics;
- the pipeline-aware Null B, where the permuted label carries
  `(surprise_std, surprise_raw, surprise_sign)` together;
- bootstrap resamples.

---

## 9. D4 — residual analysis (resolved by §1.2)

Residual MI is secondary and uses Null B only. Its validity is checked under
the selected tie policy in a **separate secondary family** 𝓡:

- **Cells:** Null B for `s8_null_t3` (heavy tail, the cell that separated
  Null A from Null B) and `s8_null_modeB_disc` (the cell where the tie policy
  matters), at n ∈ {60, 77, 80, 97, 100, 120}.
- **Rule:** the same §4 rule and batches, with the same ceiling.
- **Null A** is computed in the same replicates as an unclassified
  diagnostic.

If any 𝓡 cell is not PASS, the residual analysis is reported as
**diagnostic only**, with no inferential claims.

The other s8 null variants (mode A, mode B continuous) are not rerun. Their
surprise is continuous, so policies A and B coincide there, and their stored
Null B point rates are all ≤ 0.059. 𝓡 does not affect primary qualification.

---

## 10. D5, D6, D7, D8

### D5 — primary interval

**Correction first.** Coverage indicators of one replicate's five horizons
are not independent trials. Coverage is now recorded per horizon and per
replicate. Aggregate uncertainty treats the **replicate as the cluster**:

- the replicate mean coverage `c_r`;
- an SE of `sd(c_r)/√R`;
- a 95% interval of mean ± 1.96·SE.

Per-horizon coverage gets Wilson intervals, because its trials are
independent replicates. The old pooled-trials uncertainty is withdrawn.

The stored outputs keep only pooled means, so the GCMI coverage cells are
**re-simulated**. They use the same seeds, and the pooled point coverage must
reproduce Amendment 01 exactly.

- **Cells:** `s2_rho015/025/035_modeA/050` and `s3a_copula_monotone`.
- **Sizes:** grid n ≥ 60, plus 77 and 97 for `s2_rho035_modeA`.
- **Estimator:** GCMI only.

The frozen suitability rule stays: minimum coverage over the ρ ≥ 0.25 cells
≥ 0.90, then the smallest mean |coverage − 0.95|, computed on cluster-point
coverage.

If the stratified percentile bootstrap is retained, its wording is:
"**nominal 95% percentile bootstrap interval with empirically observed
synthetic coverage of approximately X under the tested scenarios**". It is
never "a well-calibrated 95% confidence interval".

### D6 — standardization

Both modes are retained: direct S and pipeline-faithful expanding
standardization. Primary applicability to macro surprises needs the
pipeline-faithful hard cells to pass: `s1_null_modeB` (V1) and
`s9_null_discrete` (V2), both in 𝓗. Tie behaviour is never inferred from
continuous synthetic S.

### D7 — KSG uncertainty (frozen)

KSG gets **no formal confidence interval** in v1. It reports:

- point estimates for k = 3, 5, 10;
- permutation evidence;
- null-distribution summaries;
- the k-spread;
- an optional subsampling spread, explicitly labelled "not a confidence
  interval".

### D8 — KSG max-statistic studentization (methodology revision, not a clerical fix)

- **Original frozen definition** (methodology §13.1): "If [per-horizon KSG
  null means] differ by more than 20%, switch to `(MI − null mean)/null SD`."
  This is a ratio-of-means criterion.
- **Why it was not operational.** Under independence, KSG null means are
  about 0 and change sign across horizons. In stored scenario 1h they ranged
  from about −0.0004 to 0.0003 bits, so a ratio or percentage difference of
  means is undefined or arbitrarily large.
- **Implemented replacement**, written into the config before the Amendment-01
  final run: studentize if, for any k at grid n ≥ 60,
  `(max_h − min_h null mean) > 0.20 × mean null SD` or
  `max/min null SD > 1.20`.
- **Status.** This is a methodology revision. The two definitions are **not**
  equivalent.
- **No rerun is needed.** The per-horizon null means and SDs, and both the
  raw and studentized adjusted rates, are stored for every KSG cell. Under
  the replacement rule:
  - mean spread is at most 0.009 null SD;
  - the SD ratio is at most 1.05;
  - so D8 = raw-MI max-statistic.

  KSG is sensitivity-only, and no tie-policy or isolation fix touches the
  scenario-1h KSG computation.

---

## 11. Implementation defects that must be fixed before revalidation

1. **Coverage clustering** (§10, D5).
2. **Real-data guard bypasses.** Paths are compared after `realpath`, so
   symlinks into protected trees are denied. Imports of data-reader libraries
   made *after* the guard is installed (pyarrow, fastparquet, polars,
   duckdb) are blocked, not only monkeypatched when already loaded.
   Adversarial tests use temporary fake files only.
3. **Tie-policy propagation** (§8). In Amendment 01, GCMI policy B was not
   used in the residual path.
4. **Scenario 7 truth.** Clean and contaminated targets are kept separately.
5. **Decision artifact.** It lists every hard cell with gate, scenario,
   estimator, policy, n, R, k, rate, ceiling, Wilson score z, Holm p-values,
   look and classification.

---

## 12. Targeted revalidation plan, and what is reused

| Group | Run | Why |
|---|---|---|
| A | 𝓗 V1 cells and the new V4a cell, GCMI only, §4 batches; KSG descriptive on V4a | Hard gates under the §4 rule |
| B | 𝓗 `s9_null_discrete` under A and B, §4 batches | Resolves D3 / V2 |
| C | 𝓡 Null B (and Null A diagnostic), selected policy, §4 batches | Secondary residual validity |
| D | GCMI coverage cells, original seeds | Replicate-level records were not stored |

V3 is evaluated from **stored** Amendment-01 rows. Those cells have
continuous S, A and B coincide, and none of the §11 fixes changes them. Group
D's reproduction of `s2_*` and `s3a` cross-checks the stored values.

Not rerun, and reused from stored evidence:

- Gaussian truth calibration;
- the U-shaped and heteroskedastic sensitivity;
- heavy-tail estimator bias;
- the full KSG grid and full power grid;
- V4b, from the stored `s7_*` rows with the new contaminated targets.

---

## 13. Corrections to Amendment 01 (these supersede the cited statements)

1. **Missing KSG k = 3 V4 failures.** Amendment 01 §0/§4 implied that every
   V3/V4 failure came from `s7_alt_contaminated`. That is false:
   - KSG k = 3 also failed old V4 in `s7_null_contaminated` at every grid
     n ≥ 60;
   - KSG k = 5 failed V1 at n = 60, in the scenario-1 mode A cell, which is
     not a contamination cell.

   The machine-readable failure list now enumerates every failure.
2. **Tie-policy FPR statement.** Amendment 01 §3.5 gave GCMI policy A's
   range as "0.043–0.060" and the n = 100 value as 0.0600. The stored values
   are **0.0435–0.0605**, with **0.0605** at n = 100 (ceiling 0.0597).
   Policy C's lower end is 0.0395.
3. **Overbroad "calibrated" wording.** Statements such as "controls type-I
   error", "Null B is calibrated in every variant" and the D5 label
   "calibrated" were point-estimate statements without Monte Carlo
   uncertainty. They are superseded by the PASS/INCONCLUSIVE/FAIL
   classification of §4. Where that classification is not run, they are
   point observations only.
4. **Scenario 7 interpretation.** Amendment 01 said KSG "reads the isolated
   outlier cluster as information" and fails V3 against the clean truth.
   Under the dependent contamination the true MI is about 0.23–0.29 bits, not
   0.094. KSG's 0.108–0.117 is therefore **below** the contaminated truth, so
   calling it "overestimation" relative to clean MI was a target error. The
   old V3/V4 judgements on `s7_alt` are withdrawn (§3).
5. **Coverage uncertainty.** Amendment 01 attached Wilson intervals computed
   over `replicates × horizons` as if those were independent trials, and
   called GCMI percentile coverage "well" covering, with a minimum of 0.928.
   Both are superseded by the cluster-aware analysis of §10. Coverage of about
   0.93 is **below** nominal 95%.

---

## 14. Conditional primary protocol (in force only if GCMI qualifies)

**Primary.** SPY, common support. `I_GCMI(S; R_h)` for CPI m/m, Core CPI m/m
and NFP, at `h ∈ {1, 5, 15, 30, 60}`. Reported:

- raw GCMI, the permutation-null mean and effective GCMI;
- stratified release-level permutation inference (B = 10,000);
- horizon max-T;
- Holm across the three primary panels;
- the D5 interval, with the honest empirical-coverage wording.

**Secondary.**

- QQQ replication.
- Residual `I(S; E_h)`, Null B only, and only if 𝓡 passes; otherwise
  diagnostic only.
- Matched raw versus residual on `cs_resid`.

**Nonlinear sensitivity.** KSG with k ∈ {3, 5, 10}.

**If GCMI does not qualify, real-data MI stays blocked.**
