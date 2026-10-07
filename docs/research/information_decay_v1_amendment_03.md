# M3 Information Decay v1 — Amendment 03: Fixed-Sample Monte Carlo Qualification Rule

| Field | Value |
|---|---|
| Type | **post-synthetic-calibration, pre-real-data methodology revision** (decision rule only) |
| REAL MARKET MI COMPUTED | **NO** |
| Amends | `docs/research/information_decay_v1_amendment_02.md` (committed `e18d46e1110753923df6230ec388ca453b9ecd4e`), §4 and the qualification parts of §5 and §8 only |
| Frozen history | `d976c3e…` methodology; `e18d46e…` Amendment 02; Amendment 02 is kept unchanged as an audit artifact |
| Operating-characteristic artifact | `docs/research/information_decay_v1_amendment_03_oc.json` (sha256 `3daa6077b4ab39c76465ae73a13b12b9538d9374ae8682afdac5d76a58931448`), committed with this amendment |
| Status at commit | Frozen **before** any estimator cell is reclassified under this rule |

## 0. Scope

This amendment revises **only** the Monte Carlo qualification decision rule
for hard type-I (FPR) cells. Everything else stays exactly as frozen in the
methodology and Amendment 02:

- the ceiling `c = 0.05 + 2·√(0.05·0.95/2000) = 0.0597468`;
- the synthetic scenarios and contamination definitions;
- GCMI, KSG, the tie policy (D3 = B), the estimands, `n_min`, the bootstrap
  and coverage policy;
- the V1/V2/V3/V4a definitions, the residual Null B, and every real-data
  methodology.

## 1. Why Amendment 02's rule is superseded

Amendment 02 §4 classified cells with one-sided Wilson score tests:

- Holm step-down **across cells**;
- α = 0.05 **split over five sequential looks** (α_look = 0.01);
- PASS / INCONCLUSIVE / FAIL at each 2,000-replicate look.

That rule is **statistically valid**: it controls the familywise probability
of any false PASS, across cells and looks, at 0.05. But it answers a stronger
question than qualification needs. It guards every individual PASS claim
simultaneously, when the qualification claim is a single conjunction. Its
**pre-run operating characteristic**, already recorded in Amendment 02 §4 and
recomputed here, makes it unfit for purpose:

| Configuration | Amendment 02 qualification probability |
|---|---|
| Perfectly calibrated estimator, true FPR = 0.05 in every hard cell | **≈ 0.22–0.24** |

A procedure whose type-I error equals its nominal level would therefore be
blocked about 77% of the time. **This is the reason for the revision.** It is
not being revised because GCMI produced four INCONCLUSIVE cells. The same
operating characteristic was computed and recorded before any Amendment-02
result existed.

Disclosure: the Amendment-02 revalidation results are known at the time of
writing. They were 38/42 PASS, 4/42 INCONCLUSIVE (rates 0.0531–0.0543 at
10,000 replicates) and 0 FAIL. The new rule is justified **only** by the
operating characteristics in §5, which are computed from binomial models
without any estimator output.

## 2. Qualification claim as an intersection-union test

For each required hard cell `i` with true FPR `p_i`:

`H0_i: p_i ≥ c` versus `H1_i: p_i < c`.

The estimator is acceptable iff **every** cell is acceptable:

- `H1_global = ∩_i H1_i`;
- `H0_global = ∪_i H0_i`, meaning at least one cell is unacceptable.

Qualification rejects `H0_global`. It does so only by rejecting **every**
`H0_i`.

**Proposition (intersection-union test; Berger 1982).** Let each per-cell
test φ_i be level-α for `H0_i`. Take any configuration `(p_1, …, p_m)` in
`H0_global`. Then some `j` has `p_j ≥ c`, and

`P(qualify) = P(φ_1 = … = φ_m = 1) ≤ P(φ_j = 1) ≤ α`.

The overall false-qualification probability is therefore bounded by α with
**no cross-cell multiplicity adjustment**. A Holm or Bonferroni correction
would only make the conjunction more conservative and protect nothing
further. This holds for any dependence between cells, including cells that
share code or scenario structure.

## 3. The fixed-sample rule (supersedes Amendment 02 §4 for qualification)

1. **Fixed N.** Every required hard qualification cell has exactly
   **`N_final = 10,000`** synthetic replicates: replicate indices 0–9,999 in
   the frozen seed scheme.
2. **One inferential look.** The counts at 2,000, 4,000, 6,000 and 8,000 may
   be used only for progress, runtime estimation and checkpointing. They
   produce **no** qualification decision. There is no optional stopping and
   no α spending.
3. **Cell test.** From `k` false positives (max-T panel rejections at α = 0.05)
   compute `p̂ = k/10,000` and the **one-sided 95% Wilson upper confidence
   bound**:

   `U = [p̂ + z²/(2N) + z·√(p̂(1 − p̂)/N + z²/(4N²))] / (1 + z²/N)`, with
   `z = 1.6448536`.

   - **PASS** iff `U < c`.
   - **FAIL** otherwise.
   - There is **no INCONCLUSIVE** class.

   At N = 10,000 this is equivalent to **k ≤ 558** (`U(558) = 0.059697`,
   `U(559) = 0.059800`). Both the bound and the decision are stored.
4. **Global qualification.** GCMI qualifies for primary M3 v1 inference iff
   **every required cell PASSES**, V3 passes under its unchanged point rule,
   and the implementation tests pass. **No cross-cell multiplicity adjustment
   is applied (§2).** If even one required cell FAILS, the verdict is
   **GCMI DOES NOT QUALIFY**, and no other estimator is substituted.

**Per-cell size.** The Wilson score test is approximate. Its exact
binomial size is evaluated rather than assumed: P(PASS | p) decreases in p,
and at the boundary `p = c` it is exactly **0.04896 ≤ 0.05**. Each φ_i is
therefore level-0.05 over `H0_i`, and the §2 bound applies: false
qualification ≤ 0.049.

### 3.1 Required cells (36), and cells reported only

| Group | Cells | n | Count |
|---|---|---|---|
| **V1** | `s1_null_modeA`, `s1_null_modeB`, `s6_null_t3`, `s10_regime_null` | {60, 77, 80, 97, 100, 120} | 24 |
| **V2** | `s9_null_discrete` under the **selected policy B** | same | 6 |
| **V4a** | `s7v4a_null_independent_contamination` | same | 6 |

The 6 `s9_null_discrete` policy-A cells of Amendment 02's 42-cell family are
also completed to 10,000 replicates and classified, **for reporting only**.
D3 is **not reopened**: policy B, selected under Amendment 02, stays the
selected policy whatever the policy-A cells show. If a required policy-B V2
cell FAILS, GCMI does not qualify, and the procedure does **not** switch to
policy A, because that would be outcome-driven selection.

**V3** stays a point rule and is unchanged: Amendment 02 §3.1, 30/30 PASS.

**Residual family 𝓡** (Null B, secondary) is not part of the qualification
family. Its Amendment-02 classifications stand: 12/12 PASS under the stricter
simultaneous rule, which is stronger evidence than the fixed-N rule requires.
It is not topped up. Null A remains invalid.

### 3.2 Completing cells to exactly 10,000

Cells that Amendment 02 stopped early, at 2,000–8,000 replicates, are
**continued** from their next replicate index up to 9,999. The frozen seed
derivation is used, already-computed replicates are not rerun, and simulation
code is unchanged. The Amendment-02 counts are taken from its hash-verified
artifact (`hard_gate_cells.csv`). Totals are the sum of the stored and the
top-up counts. Any cell whose final count is not exactly 10,000 makes the run
refuse to classify.

## 4. Unchanged decisions

| Decision | Status |
|---|---|
| D2 | `n_min = 60`, necessary but not sufficient |
| D3 | Policy **B** (Amendment 02; not reopened) |
| D4 | Raw primary; residual secondary with **Null B only** |
| D5 | Stratified percentile bootstrap. Wording: nominal 95% interval, about 0.93 observed synthetic coverage. Cluster-aware coverage is unchanged and not rerun. |
| D6 | Both standardization modes retained |
| D7 | No formal KSG CI |
| D8 | Raw-MI KSG max-statistic, under the replacement rule documented in Amendment 02 |

## 5. Operating characteristics (binomial simulation only; computed before reclassification)

Method:

- `true p` per cell; `N = 10,000`.
- **1,000,000** deterministic simulations per configuration for the new rule,
  checked against the **exact** product of binomial CDFs.
- 20,000 simulations per configuration for the Amendment-02 rule, using its
  own implementation.
- Seeds: `rng_for("amendment03_oc", <rule>, <configuration>)`.
- The 36 required cells come first; perturbed cells are required cells.
- Full results are in the OC artifact.

| True configuration | Amendment 02 P(qualify) | Amendment 03 P(qualify), 36 required: sim / exact | Amendment 03 P(all 42 pass) |
|---|---|---|---|
| All cells p = 0.045 | 0.9999 | 0.999993 / 0.999992 | 0.999994 |
| **All cells p = 0.05 (perfectly calibrated)** | **0.2382** | **0.862320 / 0.862477** | 0.841499 |
| All cells p = 0.053 | 0.0000 | 0.020522 / 0.020493 | 0.010685 |
| **One cell p = c (0.0597), others 0.05** | 0.0015 | **0.041992 / 0.042399** | 0.041164 |
| One cell p = 0.065 | 0.0000 | 0.000070 / 0.000065 | 0.000061 |
| One cell p = 0.07 | 0.0000 | 0 / 5.0e-9 | 0 |
| One cell p = 0.08 | 0.0000 | 0 / 3.7e-21 | 0 |
| Two cells p = c | 0.0000 | 0.002030 / 0.002084 | 0.002124 |
| Three cells p = c | 0.0000 | 0.000110 / 0.000102 | 0.000108 |
| One cell p = c, one p = 0.065 | 0.0000 | 0.000003 / 0.000003 | 0.000004 |
| All 36 required cells p = c | 0.0000 | 0 / ≈0 | 0 |

Exact per-cell P(PASS):

| True p | P(PASS) |
|---|---|
| 0.045 | 0.9999998 |
| 0.050 | 0.99590 |
| 0.053 | 0.89764 |
| **c = 0.059747** | **0.04896** |
| 0.060 | 0.03915 |
| 0.065 | 7.5e-5 |
| 0.070 | 5.0e-9 |

**What changed scientifically.**

- A perfectly calibrated estimator now qualifies with probability about
  **0.86** (it was about 0.24).
- An estimator with **any** cell at or beyond the ceiling is falsely
  qualified with probability at most about **0.049**. This is the
  intersection-union bound, attained only with a single boundary cell.
- The new rule is still demanding near nominal. Because it requires an upper
  *confidence bound* below the ceiling, a cell whose true FPR is 0.053 passes
  with probability 0.90, and a grid in which every cell has true FPR 0.053
  qualifies with probability only about 0.02.

## 6. Sequencing

1. Commit this amendment and the OC artifact only. No implementation changes
   go in this commit.
2. Top up every hard cell to exactly 10,000 replicates (§3.2).
3. Classify all cells mechanically under §3.
4. Write uncommitted results for independent review.

Real-data MI stays blocked unless GCMI qualifies **and** the independent
review accepts the calibration.
