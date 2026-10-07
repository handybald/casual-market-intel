# M3 Information Decay v1 — Amendment 04: Exact Primary-Cohort-Size Qualification

| Field | Value |
|---|---|
| Type | **post-calibration, pre-real-data exact-cohort qualification amendment** |
| REAL MARKET MI COMPUTED | **NO** |
| Builds on | methodology `d976c3e…`, Amendment 02 `e18d46e…`, Amendment 03 `cef704e…` (none edited) |
| Frozen inventory | `docs/research/information_decay_v1_amendment_04_inventory.json` (sha256 `4a40962fbf3fa211a5c9a21e64bb05b6d9ee5b1d8edc2e7c2ee5ce88a27ac058`), committed with this amendment. It contains **no simulation results**. |
| Status at commit | Frozen **before** any n = 93 or n = 103 qualification simulation is run |

## 1. Primary cohort correction

Amendment 02 made **raw MI** the primary estimand. Its primary cohort is the
SPY raw common-support cohort `cs_raw`. The sizes are recorded in methodology
§7.2, counted from inclusion flags before any MI:

| Family | Primary `cs_raw` n | Secondary residual `cs_resid` n |
|---|---|---|
| CPI m/m | **93** | 77 |
| Core CPI m/m | **93** | 77 |
| NFP | **103** | 97 |

Amendments 02 and 03 qualified the exact sizes 77 and 97. Those are the
**residual** cohort sizes, and they were carried over from the time when the
residual was primary. Exact-size applicability for the **primary** analysis
must instead use **n = 93 and n = 103**.

- The primary cohort is **not** shrunk to an already-qualified size.
- The residual family 𝓡 (Null B) was already qualified at 77 and 97
  (Amendment 02, 12/12 PASS), so the secondary analysis needs nothing
  further.

## 2. Exact-size qualification rule (frozen before execution)

**Inventory.** The inventory is derived programmatically, not asserted. It is
the required `(gate, policy)` pairs of `config/information_decay_v1_amendment03.yaml`,
crossed with the hard-family cells of `config/information_decay_v1_amendment02.yaml`,
crossed with n ∈ {93, 103}. The source config hashes are recorded in the
inventory file. Result: **12 cells**.

| Gate | Cells | Scenario (mode) | n = 93 | n = 103 |
|---|---|---|---|---|
| V1 | `s1_null_modeA` | s1_null (A) | 1 | 1 |
| V1 | `s1_null_modeB` | s1_null (B_cont) | 1 | 1 |
| V1 | `s6_null_t3` | s6_t3 (A) | 1 | 1 |
| V1 | `s10_regime_null` | s10_regime (A) | 1 | 1 |
| V2 | `s9_null_discrete`, **policy B** (`gcmi_B`) | s9_discrete (B_disc) | 1 | 1 |
| V4a | `s7v4a_null_independent_contamination` | s7v4a_indep_contam (A) | 1 | 1 |
| **Total** | | | **6** | **6** |

**Rule.** This is the unchanged Amendment-03 rule:

- each cell has exactly **N = 10,000** synthetic replicates, indices 0–9,999;
- there is **one** inferential look, with no sequential decision and no
  optional stopping;
- the statistic is the one-sided 95% Wilson upper bound, with
  z = 1.6448536269514722;
- the ceiling is **`c = 0.059746794344808965`**;
- **PASS iff U < c**, which at N = 10,000 means ≤ 558 false positives;
  otherwise FAIL;
- there is **no** Holm or Bonferroni adjustment (intersection-union,
  Amendment 03 §2).

**Unchanged.** Scenario generation, contamination, estimator, tie policy (B),
permutation counts (1,000 stratified permutations per replicate), horizons and
thresholds are all unchanged.

**Seeds.** Seeds follow the existing scheme,
`rng_for("calibration", <cell>, <n>, <rep>, <purpose>)`. The sizes 93 and 103
were never simulated before, so every replicate is new.

**One run per size, not per family.** Synthetic qualification is by
estimator, procedure and n, not by event family. **n = 93 is run once** and
covers both CPI m/m and Core CPI m/m. n = 103 covers NFP.

## 3. Consequence

Primary SPY M3 analysis may proceed **only if every one of the 12 cells
PASSES**. If any cell FAILS, the primary analysis **STOPs**. None of the
following is allowed:

- reducing the cohort size;
- choosing another estimator;
- changing the ceiling;
- substituting n = 100;
- interpolating from neighbouring n.

The verdict on the actual primary cohort sizes is then **GCMI DOES NOT
QUALIFY**.

The existing Amendment-03 evidence (36/36 PASS at n ∈ {60, 77, 80, 97, 100,
120}; V3 30/30) stays part of the calibration record. It is neither replaced
nor rewritten.

## 4. D2 unchanged

D2 stays at **n_min = 60**, a necessary general threshold. The exact-size
validation at 93 and 103 is an additional applicability requirement for the
actual primary cohorts. It does not redefine D2.

## 5. Wilson cutoff correction

The correct integer cutoff at N = 10,000 and `c = 0.059746794344808965` is
**558**:

- FP = 558 gives `U ≈ 0.059697`: **PASS**;
- FP = 559 gives `U ≈ 0.059800`: **FAIL**.

The committed Amendment 03 (§3, item 3) already states `k ≤ 558`, and a
repository-wide search finds no "548" claim. Any prose outside the repository
that gave the cutoff as 548 is superseded by this section. The
Amendment-03 qualification results are unaffected: they were computed from
the Wilson bound itself, not from an integer cutoff.

## 6. Implementation prerequisites (fixed after this commit, before the exact-size run)

1. **Real-data guard.** While the guard is active, any file operation with a
   non-null `dir_fd` is **denied (fail closed)**. Calibration code never uses
   `dir_fd`-relative access, and resolving a descriptor's path is
   platform-specific and not attempted.
2. **Implementation fingerprint.** The fingerprint is pattern-based:
   - `src/research/information_decay/**/*.py`
   - `scripts/*information_decay*.py`
   - `config/information_decay*.yaml`

   Paths are sorted and relative, each file is hashed, and an aggregate hash
   is computed. Caches, generated reports and prose are excluded. Protocol
   identity (methodology and amendment SHAs) is recorded separately.

## 7. Sequencing

1. Commit this amendment and the inventory.
2. Apply the implementation fixes (§6) and their tests.
3. Run exactly the 12 inventory cells, verifying the derived inventory
   against the committed file.
4. Classify them under §2 and record the results, uncommitted, for review.
