# M3 Information Decay v1 — Amendment 04 Exact-Size Qualification Results

| Field | Value |
|---|---|
| Protocol | Amendment 04 + inventory, committed **before** these runs at `9f4bef157cd490f848cb3544ab92475def430edc`. Verified unchanged at run time, together with the methodology (`d976c3e`), Amendment 02 (`e18d46e`), Amendment 03 and its OC file (`cef704e`). |
| REAL MARKET MI COMPUTED | **NO** (synthetic only; hardened `real_data_guard`, including fail-closed `dir_fd` handling) |
| Status | Results for review — **uncommitted** |
| Inventory | Derived programmatically; equal to the committed file (sha256 `4a40962f…c058`) |
| Implementation fingerprint | Pattern-based: 23 files, aggregate `82e5f46ccd41920106aa0657bbcdc4727f21a751b09fe4bec78f494e9473a1e4` |
| Outputs | `data/reports/information_decay/amendment04_exact_size/` (combined sha256 `16080b14…4314`); metadata `metadata/research/information_decay_amendment04_exact_size.json` |
| Run | 12 cells × 10,000 replicates (indices 0–9,999), one look; 220 s wall time at 8 workers |

## Result

**PRIMARY SPY COHORT SIZES QUALIFY**: 12 / 12 PASS, 0 FAIL.

This gives **GCMI QUALIFIES FOR PRIMARY M3 V1 ON THE ACTUAL PRIMARY COHORT
SIZES**, combined with the Amendment-03 evidence (36/36 hard cells PASS, V3
30/30).

| Gate | Cell | Scenario | n | Estimator | Policy | Reps | FP | p̂ | U95 (one-sided) | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| V1 | s1_null_modeA | s1_null | 93 | gcmi | A | 10,000 | 492 | 0.0492 | 0.05288 | PASS |
| V1 | s1_null_modeB | s1_null | 93 | gcmi | A | 10,000 | 505 | 0.0505 | 0.05422 | PASS |
| V1 | s6_null_t3 | s6_t3 | 93 | gcmi | A | 10,000 | 506 | 0.0506 | 0.05433 | PASS |
| V1 | s10_regime_null | s10_regime | 93 | gcmi | A | 10,000 | 474 | 0.0474 | 0.05102 | PASS |
| V2 | s9_null_discrete | s9_discrete | 93 | gcmi_B | B | 10,000 | 499 | 0.0499 | 0.05360 | PASS |
| V4a | s7v4a_null_independent_contamination | s7v4a_indep_contam | 93 | gcmi | A | 10,000 | 506 | 0.0506 | 0.05433 | PASS |
| V1 | s1_null_modeA | s1_null | 103 | gcmi | A | 10,000 | 491 | 0.0491 | 0.05278 | PASS |
| V1 | s1_null_modeB | s1_null | 103 | gcmi | A | 10,000 | 513 | 0.0513 | 0.05505 | PASS |
| V1 | s6_null_t3 | s6_t3 | 103 | gcmi | A | 10,000 | 516 | 0.0516 | 0.05536 | PASS |
| V1 | s10_regime_null | s10_regime | 103 | gcmi | A | 10,000 | 490 | 0.0490 | 0.05267 | PASS |
| V2 | s9_null_discrete | s9_discrete | 103 | gcmi_B | B | 10,000 | **555** | 0.0555 | **0.05939** | PASS |
| V4a | s7v4a_null_independent_contamination | s7v4a_indep_contam | 103 | gcmi | A | 10,000 | 496 | 0.0496 | 0.05329 | PASS |

The ceiling is 0.059746794344808965. PASS means at most 558 false
positives.

**Reviewer note.** V2 at n = 103 is the closest cell of the whole
calibration: 555 against the 558 cutoff. It passes under the frozen rule.
Per-cell seed identities and row hashes are in `exact_size_table.csv`.
