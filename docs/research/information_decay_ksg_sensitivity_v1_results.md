# M3 Information Decay v1 — KSG Estimator Sensitivity: Result Record

**Status: independently reviewed and accepted after a reporting-only
correction** (verdict: "KSG SENSITIVITY RESULTS NEED ONE FIX", resolved in
this record).

This record is **descriptive scientific reporting only** and introduces no
methodology. No estimator, inference, seed or configuration was changed. The
reported values are read from the accepted machine-readable outputs in
`data/reports/information_decay/ksg_sensitivity_v1/`.

## A. Objective and secondary sensitivity status

KSG was used to check whether the macro-surprise / market-response dependence
identified with the frozen primary estimator (GCMI, tie policy B) is
**qualitatively** supported by a nonparametric nearest-neighbour MI estimator.
The questions are whether KSG detects CPI m/m, Core CPI m/m and NFP
dependence, whether the information profiles agree, and how much the answers
depend on k.

KSG is a **secondary, exploratory, sensitivity-only** estimator (Amendment 02
§6):

- It is not, and is not promoted to, a primary estimator.
- No primary claim rests on KSG significance.
- KSG and GCMI are **not equally calibrated**, so their numerical magnitudes
  are **not** compared as like quantities.

## B. Frozen provenance

| Item | Value |
|---|---|
| Calibration freeze | `m3-information-decay-calibration-freeze` → `f08fe850c142fed595e847ce03d6931908587c6d` |
| SPY primary | `m3-primary-spy-information-profile-v1` → `70ee4c1f54cb36eb7808fb5a647e5b7f2cd2aa71` (outputs `31a05df0…9b60`, intact) |
| SPY residual | `m3-secondary-spy-residual-profile-v1` → `eb2948bcafba8f5a2393efdcb47d81a8267ea5af` (outputs `2216d6c7…02fc4`, intact) |
| QQQ replication | `m3-qqq-replication-v1` → `b1ec964da005113ba87cf2c2f089a0bfccb45c1e` (outputs `3d5e8f59…8932`, intact) |
| Protocol commits | methodology `d976c3e408ec770ffa6633af22b422d3814e0e16`; Amendment 02 `e18d46e1110753923df6230ec388ca453b9ecd4e`; Amendment 03 `cef704ed34d971487ddcc5e9525da2ca1735e77a`; Amendment 04 `9f4bef157cd490f848cb3544ab92475def430edc` |
| KSG runner | `scripts/run_information_decay_ksg_sensitivity_v1.py`, commit **`286d93c159b2c33639247be61b11845b7b52b900`** (code only; the run's HEAD). The frozen estimator library is byte-identical to the calibration tag. |
| Implementation fingerprint | `0a1e00e447c92073ec7639789f97e37e9af131b21bfc1376b2cb8b9a629ce538` (31 files; re-verified against the current files) |
| Config | `config/information_decay_ksg_sensitivity_v1.yaml`, sha256 `7192f2e565f254f8e66a06cda8218413b00a4af2c9b1dc83ba55ea0ff28038b0` (re-verified) |
| Dataset | content sha256 `a6f83a05be0699032dba64e1d712d8d5d6f132dcdf154db8e890fd571c049986` (re-verified) |
| Run timestamp | 2026-10-08T21:42:36Z |

## C. Cohort verification

These are only the accepted raw common-support cohorts. Each release-ID
SHA-256 equals the frozen metadata, and all five horizons use identical
releases within each symbol × family.

| Symbol | Family | n | Release-ID sha256 |
|---|---|---|---|
| SPY | CPI m/m | 93 | `17131c2dd66ffdcc6c04324db775b07cd26fbd6e1d39f983e1f9078b67060bb7` |
| SPY | Core CPI m/m | 93 | `17131c2dd66ffdcc6c04324db775b07cd26fbd6e1d39f983e1f9078b67060bb7` |
| SPY | NFP | 103 | `74bab3d4361770b607dfd6ce830f5340cb25ca4cf6dba990d581531eb4a06e94` |
| QQQ | CPI m/m | 80 | `abac905eec34ccb91aad65680ab2138b73b0ce8c614b8229a399001cb0ca1866` |
| QQQ | Core CPI m/m | 80 | `abac905eec34ccb91aad65680ab2138b73b0ce8c614b8229a399001cb0ca1866` |
| QQQ | NFP | 89 | `e12795fdd5b018569ac79058d74073b8176dba4c2262d745c2fbe6b435caadf3` |

SPY and QQQ were analysed separately and never pooled. No residual KSG was
computed.

## D. KSG estimator definition (frozen)

- **Algorithm:** KSG algorithm 1 (Kraskov, Stögbauer & Grassberger 2004),
  max-norm, at **k = 3, 5, 10**, fixed for every panel and never tuned. It
  uses the frozen `src/research/information_decay/ksg.py`. Results are in
  bits.
- **Scaling and ties:** each variable is z-scored. Seeded jitter of sd 1e-10
  in z-units breaks exact ties only. The jitter key adds symbol, response and
  `estimator=ksg_k{k}`.
- **Tie policy K-A** (Amendment 01 D3): the standardized surprise
  `surprise_std` is used as given.
- **Permutations:** 10,000 stratified release-level permutations within
  2016–2020, 2021–2022 and 2023–2026-09, shared across the five horizons.
  The permutation keys carry no estimator, so these are **the same
  permutation draws as the frozen GCMI runs**.
- **Effective KSG** = observed − permutation-null mean, not truncated.
- **Horizon max-statistic** on raw MI (D8). p = (1 + count)/(B + 1).
- **No confidence intervals** (D7), and **no Holm** correction across KSG
  panels.

## E. KSG calibration limitations (synthetic; Amendments 01–02)

- **Lower power for monotonic dependence.** At n = 80–100 and ρ = 0.35,
  horizon-adjusted panel power was about 0.31–0.74 for KSG against
  0.97–0.99 for GCMI.
- **Sensitivity to non-Gaussian marginals.** Under monotone non-Gaussian
  marginals KSG recovered about 35–55% of the true MI.
- **Tie and atom sensitivity.** Discrete or near-discrete surprises and
  duplicated observations affect nearest-neighbour counting.
- **No calibrated interval.** KSG bootstrap percentile intervals collapsed,
  and subsampling under-covered.
- **Detection of non-monotonic and heteroskedastic structure.** KSG
  partially detects U-shaped and heteroskedastic dependence where GCMI is
  blind.

## F. SPY results

All values in bits. Effective = observed − null mean, not truncated.
p = 0.0001 is the floor 1/10,001.

| Family | h (min) | k | KSG observed | Null mean | Null SD | **Effective** | p (pointwise) | p (horizon-adj.) |
|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 3 | 0.3447 | +0.1026 | 0.0976 | **+0.2420** | 0.0125 | 0.0185 |
| CPI m/m | 5 | 3 | 0.3154 | +0.0606 | 0.0926 | **+0.2549** | 0.0070 | 0.0330 |
| CPI m/m | 15 | 3 | 0.2217 | +0.0389 | 0.0868 | **+0.1828** | 0.0254 | 0.1903 |
| CPI m/m | 30 | 3 | 0.4924 | +0.0271 | 0.0938 | **+0.4654** | 0.0001 | 0.0005 |
| CPI m/m | 60* | 3 | 0.2353 | -0.0165 | 0.0873 | **+0.2518** | 0.0055 | 0.1509 |
| CPI m/m | 1 | 5 | 0.3915 | +0.0807 | 0.0702 | **+0.3107** | 0.0002 | 0.0002 |
| CPI m/m | 5 | 5 | 0.2903 | +0.0470 | 0.0697 | **+0.2433** | 0.0018 | 0.0065 |
| CPI m/m | 15 | 5 | 0.2686 | +0.0406 | 0.0673 | **+0.2280** | 0.0014 | 0.0125 |
| CPI m/m | 30 | 5 | 0.3783 | +0.0107 | 0.0649 | **+0.3676** | 0.0001 | 0.0002 |
| CPI m/m | 60* | 5 | 0.1595 | -0.0098 | 0.0613 | **+0.1693** | 0.0085 | 0.2194 |
| CPI m/m | 1 | 10 | 0.3105 | +0.0357 | 0.0459 | **+0.2748** | 0.0001 | 0.0001 |
| CPI m/m | 5 | 10 | 0.2598 | +0.0307 | 0.0464 | **+0.2291** | 0.0002 | 0.0005 |
| CPI m/m | 15 | 10 | 0.3043 | +0.0340 | 0.0445 | **+0.2703** | 0.0001 | 0.0001 |
| CPI m/m | 30 | 10 | 0.3584 | +0.0309 | 0.0410 | **+0.3275** | 0.0001 | 0.0001 |
| CPI m/m | 60* | 10 | 0.2043 | +0.0152 | 0.0392 | **+0.1891** | 0.0001 | 0.0043 |
| Core CPI m/m | 1 | 3 | 0.5754 | +0.0739 | 0.1046 | **+0.5015** | 0.0001 | 0.0001 |
| Core CPI m/m | 5 | 3 | 0.3844 | +0.0445 | 0.0962 | **+0.3399** | 0.0007 | 0.0054 |
| Core CPI m/m | 15 | 3 | 0.3760 | +0.0244 | 0.0931 | **+0.3516** | 0.0005 | 0.0066 |
| Core CPI m/m | 30 | 3 | 0.2359 | +0.0132 | 0.0974 | **+0.2228** | 0.0193 | 0.1248 |
| Core CPI m/m | 60* | 3 | 0.0381 | -0.0123 | 0.0886 | **+0.0505** | 0.2711 | 0.8978 |
| Core CPI m/m | 1 | 5 | 0.4640 | +0.0707 | 0.0743 | **+0.3933** | 0.0001 | 0.0001 |
| Core CPI m/m | 5 | 5 | 0.3600 | +0.0433 | 0.0716 | **+0.3167** | 0.0002 | 0.0009 |
| Core CPI m/m | 15 | 5 | 0.2418 | +0.0351 | 0.0699 | **+0.2067** | 0.0056 | 0.0327 |
| Core CPI m/m | 30 | 5 | 0.2328 | +0.0103 | 0.0655 | **+0.2225** | 0.0033 | 0.0416 |
| Core CPI m/m | 60* | 5 | 0.1149 | -0.0083 | 0.0622 | **+0.1233** | 0.0358 | 0.4290 |
| Core CPI m/m | 1 | 10 | 0.3299 | +0.0464 | 0.0416 | **+0.2835** | 0.0001 | 0.0001 |
| Core CPI m/m | 5 | 10 | 0.2669 | +0.0325 | 0.0421 | **+0.2344** | 0.0003 | 0.0004 |
| Core CPI m/m | 15 | 10 | 0.2007 | +0.0334 | 0.0423 | **+0.1673** | 0.0010 | 0.0041 |
| Core CPI m/m | 30 | 10 | 0.1689 | +0.0231 | 0.0399 | **+0.1458** | 0.0020 | 0.0163 |
| Core CPI m/m | 60* | 10 | 0.1240 | +0.0131 | 0.0386 | **+0.1109** | 0.0077 | 0.0942 |
| NFP | 1 | 3 | 0.0092 | -0.0304 | 0.0611 | **+0.0397** | 0.2492 | 0.8810 |
| NFP | 5 | 3 | 0.0373 | +0.0252 | 0.0632 | **+0.0122** | 0.4022 | 0.7013 |
| NFP | 15 | 3 | -0.0150 | -0.0188 | 0.0603 | **+0.0038** | 0.4554 | 0.9652 |
| NFP | 30 | 3 | 0.0581 | -0.0073 | 0.0600 | **+0.0655** | 0.1382 | 0.5283 |
| NFP | 60* | 3 | -0.0239 | -0.0151 | 0.0597 | **-0.0088** | 0.5366 | 0.9813 |
| NFP | 1 | 5 | -0.0131 | -0.0396 | 0.0391 | **+0.0265** | 0.2397 | 0.9377 |
| NFP | 5 | 5 | -0.0118 | +0.0056 | 0.0423 | **-0.0174** | 0.6498 | 0.9315 |
| NFP | 15 | 5 | -0.0822 | -0.0131 | 0.0421 | **-0.0691** | 0.9632 | 1.0000 |
| NFP | 30 | 5 | -0.0509 | -0.0117 | 0.0399 | **-0.0392** | 0.8395 | 0.9994 |
| NFP | 60* | 5 | -0.0037 | -0.0205 | 0.0387 | **+0.0168** | 0.3160 | 0.8819 |
| NFP | 1 | 10 | -0.0267 | -0.0371 | 0.0225 | **+0.0104** | 0.3072 | 0.9665 |
| NFP | 5 | 10 | 0.0122 | -0.0123 | 0.0264 | **+0.0245** | 0.1704 | 0.4021 |
| NFP | 15 | 10 | 0.0105 | -0.0026 | 0.0236 | **+0.0131** | 0.2740 | 0.4377 |
| NFP | 30 | 10 | -0.0066 | -0.0353 | 0.0238 | **+0.0287** | 0.1134 | 0.7657 |
| NFP | 60* | 10 | 0.0355 | -0.0258 | 0.0231 | **+0.0614** | 0.0087 | 0.1006 |

\* 60 min is session-boundary-adjacent (08:30 ET releases; the window ends at
the 09:30 regular-session open).

## G. QQQ results

| Family | h (min) | k | KSG observed | Null mean | Null SD | **Effective** | p (pointwise) | p (horizon-adj.) |
|---|---|---|---|---|---|---|---|---|
| CPI m/m | 1 | 3 | 0.2589 | +0.0239 | 0.0933 | **+0.2350** | 0.0114 | 0.0338 |
| CPI m/m | 5 | 3 | 0.3880 | +0.0130 | 0.0964 | **+0.3750** | 0.0009 | 0.0017 |
| CPI m/m | 15 | 3 | 0.4221 | -0.0197 | 0.0905 | **+0.4418** | 0.0003 | 0.0007 |
| CPI m/m | 30 | 3 | 0.4815 | -0.0087 | 0.0944 | **+0.4902** | 0.0001 | 0.0002 |
| CPI m/m | 60* | 3 | 0.2762 | -0.0107 | 0.0919 | **+0.2869** | 0.0039 | 0.0234 |
| CPI m/m | 1 | 5 | 0.2714 | +0.0194 | 0.0689 | **+0.2520** | 0.0013 | 0.0035 |
| CPI m/m | 5 | 5 | 0.2880 | +0.0195 | 0.0666 | **+0.2685** | 0.0001 | 0.0016 |
| CPI m/m | 15 | 5 | 0.3734 | +0.0120 | 0.0634 | **+0.3614** | 0.0001 | 0.0003 |
| CPI m/m | 30 | 5 | 0.4185 | +0.0117 | 0.0657 | **+0.4068** | 0.0001 | 0.0002 |
| CPI m/m | 60* | 5 | 0.3387 | +0.0134 | 0.0653 | **+0.3254** | 0.0001 | 0.0005 |
| CPI m/m | 1 | 10 | 0.2479 | +0.0300 | 0.0483 | **+0.2180** | 0.0003 | 0.0015 |
| CPI m/m | 5 | 10 | 0.3018 | +0.0324 | 0.0494 | **+0.2694** | 0.0001 | 0.0002 |
| CPI m/m | 15 | 10 | 0.3396 | +0.0287 | 0.0486 | **+0.3109** | 0.0001 | 0.0001 |
| CPI m/m | 30 | 10 | 0.4271 | +0.0308 | 0.0424 | **+0.3963** | 0.0001 | 0.0001 |
| CPI m/m | 60* | 10 | 0.3308 | +0.0286 | 0.0429 | **+0.3022** | 0.0001 | 0.0001 |
| Core CPI m/m | 1 | 3 | 0.4571 | +0.0070 | 0.0970 | **+0.4501** | 0.0001 | 0.0003 |
| Core CPI m/m | 5 | 3 | 0.4078 | +0.0033 | 0.0975 | **+0.4045** | 0.0003 | 0.0007 |
| Core CPI m/m | 15 | 3 | 0.1557 | -0.0180 | 0.0895 | **+0.1737** | 0.0359 | 0.2223 |
| Core CPI m/m | 30 | 3 | 0.2350 | -0.0076 | 0.0955 | **+0.2426** | 0.0110 | 0.0543 |
| Core CPI m/m | 60* | 3 | 0.2180 | -0.0087 | 0.0936 | **+0.2266** | 0.0146 | 0.0751 |
| Core CPI m/m | 1 | 5 | 0.3201 | +0.0174 | 0.0703 | **+0.3028** | 0.0003 | 0.0007 |
| Core CPI m/m | 5 | 5 | 0.3993 | +0.0165 | 0.0676 | **+0.3828** | 0.0001 | 0.0002 |
| Core CPI m/m | 15 | 5 | 0.2780 | +0.0097 | 0.0668 | **+0.2683** | 0.0006 | 0.0024 |
| Core CPI m/m | 30 | 5 | 0.2500 | +0.0095 | 0.0673 | **+0.2405** | 0.0009 | 0.0069 |
| Core CPI m/m | 60* | 5 | 0.2274 | +0.0054 | 0.0671 | **+0.2220** | 0.0027 | 0.0157 |
| Core CPI m/m | 1 | 10 | 0.3188 | +0.0264 | 0.0450 | **+0.2925** | 0.0001 | 0.0002 |
| Core CPI m/m | 5 | 10 | 0.2859 | +0.0277 | 0.0477 | **+0.2581** | 0.0001 | 0.0003 |
| Core CPI m/m | 15 | 10 | 0.2146 | +0.0260 | 0.0478 | **+0.1886** | 0.0025 | 0.0058 |
| Core CPI m/m | 30 | 10 | 0.2076 | +0.0253 | 0.0451 | **+0.1823** | 0.0010 | 0.0078 |
| Core CPI m/m | 60* | 10 | 0.1838 | +0.0214 | 0.0442 | **+0.1624** | 0.0018 | 0.0156 |
| NFP | 1 | 3 | -0.0891 | -0.0044 | 0.0569 | **-0.0846** | 0.9402 | 1.0000 |
| NFP | 5 | 3 | 0.0253 | -0.0055 | 0.0560 | **+0.0309** | 0.2822 | 0.8245 |
| NFP | 15 | 3 | -0.0699 | +0.0014 | 0.0586 | **-0.0713** | 0.8977 | 0.9999 |
| NFP | 30 | 3 | 0.0436 | +0.0302 | 0.0526 | **+0.0134** | 0.3766 | 0.6733 |
| NFP | 60* | 3 | 0.0344 | -0.0364 | 0.0523 | **+0.0708** | 0.0921 | 0.7559 |
| NFP | 1 | 5 | -0.0624 | -0.0348 | 0.0362 | **-0.0276** | 0.7694 | 1.0000 |
| NFP | 5 | 5 | -0.0413 | -0.0264 | 0.0365 | **-0.0150** | 0.6415 | 0.9987 |
| NFP | 15 | 5 | -0.0256 | +0.0060 | 0.0383 | **-0.0315** | 0.7951 | 0.9917 |
| NFP | 30 | 5 | -0.0316 | +0.0164 | 0.0360 | **-0.0480** | 0.9160 | 0.9964 |
| NFP | 60* | 5 | -0.0238 | -0.0372 | 0.0325 | **+0.0135** | 0.3274 | 0.9903 |
| NFP | 1 | 10 | -0.0678 | -0.0278 | 0.0232 | **-0.0400** | 0.9587 | 1.0000 |
| NFP | 5 | 10 | -0.0428 | -0.0312 | 0.0224 | **-0.0115** | 0.6891 | 0.9942 |
| NFP | 15 | 10 | -0.0578 | -0.0332 | 0.0248 | **-0.0246** | 0.8381 | 0.9998 |
| NFP | 30 | 10 | -0.0511 | -0.0241 | 0.0214 | **-0.0270** | 0.9010 | 0.9996 |
| NFP | 60* | 10 | -0.0321 | -0.0220 | 0.0219 | **-0.0101** | 0.6690 | 0.9569 |

## H. Corrected GCMI / KSG agreement classification

The universe is 2 symbols × 3 families × 5 horizons = **30** cells. The
classification uses **descriptive horizon-adjusted p ≤ 0.05 labels**: frozen
GCMI horizon-adjusted p against KSG horizon-adjusted p for each k. **KSG
remains sensitivity-only**, and the labels are not confirmatory decisions.

| Classification | Cells |
|---|---|
| Both GCMI and KSG (all k) | **13** |
| GCMI and KSG (some k) | **6** |
| GCMI only | **1** |
| KSG only | **0** |
| Neither | **10** |
| **Total** | **30** |

*This supersedes an erroneous 35-cell classification (17 / 7 / 1 / 10) given
in the pre-review run summary. The machine-readable `ksg_vs_gcmi.csv`
(30 rows) was always correct.*

| Symbol | Family | h (min) | GCMI effective (frozen) | GCMI p adj. | KSG k=3 p adj. | KSG k=5 p adj. | KSG k=10 p adj. | Classification |
|---|---|---|---|---|---|---|---|---|
| SPY | CPI m/m | 1 | 0.4889 | 0.0001 | 0.0185 | 0.0002 | 0.0001 | Both GCMI and KSG (all k) |
| SPY | CPI m/m | 5 | 0.4420 | 0.0001 | 0.0330 | 0.0065 | 0.0005 | Both GCMI and KSG (all k) |
| SPY | CPI m/m | 15 | 0.3334 | 0.0001 | 0.1903 | 0.0125 | 0.0001 | GCMI and KSG (some k) |
| SPY | CPI m/m | 30 | 0.3092 | 0.0001 | 0.0005 | 0.0002 | 0.0001 | Both GCMI and KSG (all k) |
| SPY | CPI m/m | 60 | 0.2648 | 0.0001 | 0.1509 | 0.2194 | 0.0043 | GCMI and KSG (some k) |
| SPY | Core CPI m/m | 1 | 0.4929 | 0.0001 | 0.0001 | 0.0001 | 0.0001 | Both GCMI and KSG (all k) |
| SPY | Core CPI m/m | 5 | 0.4344 | 0.0001 | 0.0054 | 0.0009 | 0.0004 | Both GCMI and KSG (all k) |
| SPY | Core CPI m/m | 15 | 0.2871 | 0.0001 | 0.0066 | 0.0327 | 0.0041 | Both GCMI and KSG (all k) |
| SPY | Core CPI m/m | 30 | 0.2456 | 0.0001 | 0.1248 | 0.0416 | 0.0163 | GCMI and KSG (some k) |
| SPY | Core CPI m/m | 60 | 0.2387 | 0.0001 | 0.8978 | 0.4290 | 0.0942 | GCMI only |
| SPY | NFP | 1 | 0.0150 | 0.1401 | 0.8810 | 0.9377 | 0.9665 | Neither |
| SPY | NFP | 5 | 0.0028 | 0.4338 | 0.7013 | 0.9315 | 0.4021 | Neither |
| SPY | NFP | 15 | 0.0106 | 0.1979 | 0.9652 | 1.0000 | 0.4377 | Neither |
| SPY | NFP | 30 | 0.0113 | 0.2000 | 0.5283 | 0.9994 | 0.7657 | Neither |
| SPY | NFP | 60 | 0.0146 | 0.1420 | 0.9813 | 0.8819 | 0.1006 | Neither |
| QQQ | CPI m/m | 1 | 0.5438 | 0.0001 | 0.0338 | 0.0035 | 0.0015 | Both GCMI and KSG (all k) |
| QQQ | CPI m/m | 5 | 0.5018 | 0.0001 | 0.0017 | 0.0016 | 0.0002 | Both GCMI and KSG (all k) |
| QQQ | CPI m/m | 15 | 0.3963 | 0.0001 | 0.0007 | 0.0003 | 0.0001 | Both GCMI and KSG (all k) |
| QQQ | CPI m/m | 30 | 0.3623 | 0.0001 | 0.0002 | 0.0002 | 0.0001 | Both GCMI and KSG (all k) |
| QQQ | CPI m/m | 60 | 0.3167 | 0.0001 | 0.0234 | 0.0005 | 0.0001 | Both GCMI and KSG (all k) |
| QQQ | Core CPI m/m | 1 | 0.5727 | 0.0001 | 0.0003 | 0.0007 | 0.0002 | Both GCMI and KSG (all k) |
| QQQ | Core CPI m/m | 5 | 0.5363 | 0.0001 | 0.0007 | 0.0002 | 0.0003 | Both GCMI and KSG (all k) |
| QQQ | Core CPI m/m | 15 | 0.3974 | 0.0001 | 0.2223 | 0.0024 | 0.0058 | GCMI and KSG (some k) |
| QQQ | Core CPI m/m | 30 | 0.3416 | 0.0001 | 0.0543 | 0.0069 | 0.0078 | GCMI and KSG (some k) |
| QQQ | Core CPI m/m | 60 | 0.3401 | 0.0001 | 0.0751 | 0.0157 | 0.0156 | GCMI and KSG (some k) |
| QQQ | NFP | 1 | -0.0020 | 0.7548 | 1.0000 | 1.0000 | 1.0000 | Neither |
| QQQ | NFP | 5 | 0.0051 | 0.3830 | 0.8245 | 0.9987 | 0.9942 | Neither |
| QQQ | NFP | 15 | 0.0009 | 0.5229 | 0.9999 | 0.9917 | 0.9998 | Neither |
| QQQ | NFP | 30 | -0.0051 | 0.9345 | 0.6733 | 0.9964 | 0.9996 | Neither |
| QQQ | NFP | 60 | -0.0072 | 1.0000 | 0.7559 | 0.9903 | 0.9569 | Neither |

## I. Panel-level detection

**All four CPI-family symbol/family panels (SPY CPI, SPY Core CPI, QQQ CPI,
QQQ Core CPI) show at least one detectable horizon for each k = 3, 5, 10:
12 of 12 CPI-family panel/k combinations.** This does **not** mean every
horizon is detected.

**NFP:** there is no horizon-adjusted KSG detection for either symbol at any
k.

No statistical independence between panels is claimed (§P).

| Symbol | Family | k | Panel p (min horizon-adj.) | Horizons with horizon-adj. p ≤ 0.05 | Peak effective horizon |
|---|---|---|---|---|---|
| SPY | CPI m/m | 3 | 0.0005 | 1, 5, 30 | 30m |
| SPY | CPI m/m | 5 | 0.0002 | 1, 5, 15, 30 | 30m |
| SPY | CPI m/m | 10 | 0.0001 | 1, 5, 15, 30, 60 | 30m |
| SPY | Core CPI m/m | 3 | 0.0001 | 1, 5, 15 | 1m |
| SPY | Core CPI m/m | 5 | 0.0001 | 1, 5, 15, 30 | 1m |
| SPY | Core CPI m/m | 10 | 0.0001 | 1, 5, 15, 30 | 1m |
| SPY | NFP | 3 | 0.5283 | none | 30m |
| SPY | NFP | 5 | 0.8819 | none | 1m |
| SPY | NFP | 10 | 0.1006 | none | 60m |
| QQQ | CPI m/m | 3 | 0.0002 | 1, 5, 15, 30, 60 | 30m |
| QQQ | CPI m/m | 5 | 0.0002 | 1, 5, 15, 30, 60 | 30m |
| QQQ | CPI m/m | 10 | 0.0001 | 1, 5, 15, 30, 60 | 30m |
| QQQ | Core CPI m/m | 3 | 0.0003 | 1, 5 | 1m |
| QQQ | Core CPI m/m | 5 | 0.0002 | 1, 5, 15, 30, 60 | 5m |
| QQQ | Core CPI m/m | 10 | 0.0002 | 1, 5, 15, 30, 60 | 1m |
| QQQ | NFP | 3 | 0.6733 | none | 60m |
| QQQ | NFP | 5 | 0.9903 | none | 60m |
| QQQ | NFP | 10 | 0.9569 | none | 60m |

## J. Horizon-profile differences

- **GCMI:** CPI-family effective estimates peak at **1 minute** in both SPY
  and QQQ.
- **KSG:** does **not** reproduce this profile uniformly.
  - Effective KSG for **CPI m/m peaks at 30 minutes in both SPY and QQQ, for
    every tested k**.
  - **Core CPI m/m** behaves differently. SPY peaks at 1 minute for every k.
    QQQ peaks at 1 minute for k = 3 and k = 10, and at 5 minutes for k = 5.

Part of the CPI difference comes from the permutation-null subtraction. The
KSG null means differ across horizons (§K). For example, SPY CPI m/m at
k = 5 has a null mean of +0.0807 bits at 1 minute and −0.0098 bits at
60 minutes.

No horizon-difference test was performed, and none of the following is
claimed:

- confirmed decay;
- confirmed non-monotonicity;
- exponential decay or an information half-life;
- an estimator-independent horizon shape.

**Supported conclusion: the presence of CPI-family dependence is
qualitatively robust across estimators, but the temporal information profile
is not.**

## K. D8 null-mean imbalance

Frozen D8 (Amendment 02) defines two diagnostics of per-horizon null
imbalance:

- **A** = (max null mean − min null mean) / mean null SD, with threshold
  **A > 0.20**;
- **B** = max null SD / min null SD, with threshold **B > 1.20**.

In synthetic calibration neither was exceeded, so D8 froze the **raw-MI
max-statistic**. On the real data:

- **14 of 18** symbol/family/k combinations exceed A;
- **0 of 18** exceed B.

| Symbol | Family | k | Null-mean range (bits) | Mean null SD | A = spread / mean SD | A > 0.20 | B = max SD / min SD | B > 1.20 |
|---|---|---|---|---|---|---|---|---|
| SPY | CPI m/m | 3 | -0.0165 … +0.1026 | 0.0916 | 1.301 | **yes** | 1.124 | no |
| SPY | CPI m/m | 5 | -0.0098 … +0.0807 | 0.0667 | 1.357 | **yes** | 1.145 | no |
| SPY | CPI m/m | 10 | +0.0152 … +0.0357 | 0.0434 | 0.473 | **yes** | 1.183 | no |
| SPY | Core CPI m/m | 3 | -0.0123 … +0.0739 | 0.0960 | 0.899 | **yes** | 1.181 | no |
| SPY | Core CPI m/m | 5 | -0.0083 … +0.0707 | 0.0687 | 1.150 | **yes** | 1.195 | no |
| SPY | Core CPI m/m | 10 | +0.0131 … +0.0464 | 0.0409 | 0.815 | **yes** | 1.094 | no |
| SPY | NFP | 3 | -0.0304 … +0.0252 | 0.0608 | 0.914 | **yes** | 1.059 | no |
| SPY | NFP | 5 | -0.0396 … +0.0056 | 0.0404 | 1.117 | **yes** | 1.093 | no |
| SPY | NFP | 10 | -0.0371 … -0.0026 | 0.0239 | 1.446 | **yes** | 1.175 | no |
| QQQ | CPI m/m | 3 | -0.0197 … +0.0239 | 0.0933 | 0.467 | **yes** | 1.064 | no |
| QQQ | CPI m/m | 5 | +0.0117 … +0.0195 | 0.0660 | 0.119 | no | 1.087 | no |
| QQQ | CPI m/m | 10 | +0.0286 … +0.0324 | 0.0463 | 0.083 | no | 1.165 | no |
| QQQ | Core CPI m/m | 3 | -0.0180 … +0.0070 | 0.0946 | 0.265 | **yes** | 1.089 | no |
| QQQ | Core CPI m/m | 5 | +0.0054 … +0.0174 | 0.0678 | 0.177 | no | 1.052 | no |
| QQQ | Core CPI m/m | 10 | +0.0214 … +0.0277 | 0.0460 | 0.137 | no | 1.081 | no |
| QQQ | NFP | 3 | -0.0364 … +0.0302 | 0.0553 | 1.205 | **yes** | 1.121 | no |
| QQQ | NFP | 5 | -0.0372 … +0.0164 | 0.0359 | 1.493 | **yes** | 1.178 | no |
| QQQ | NFP | 10 | -0.0332 … -0.0220 | 0.0227 | 0.493 | **yes** | 1.159 | no |

**The pre-frozen raw-MI max-statistic was applied exactly as specified.** No
studentization or other modification was made.

## L. Max-statistic interpretation limitations

Because of the D8 imbalance:

- **Null means vary across horizons** within a panel. The raw-MI
  max-statistic is dominated by the horizons with high null means.
- **Horizon-level detection power can differ** between horizons. Horizons
  with low null means are penalized by the max-null distribution.
- **Horizon-adjusted p-values should not be interpreted as equal-power
  comparisons** between horizons.
- **Global panel-level inference** (is there any detectable horizon) relies
  on the frozen stratified exchangeability assumptions, under which the
  max-statistic test of the global null remains valid.
- **Strong horizon-wise FWER under partial alternatives was not
  established** for this imbalanced setting.
- **No horizon-difference test was performed.**

## M. Tie / jitter considerations

- The KSG K-A representation, `surprise_std` as given, **differs from GCMI
  tie policy B**, which ranks within raw-surprise tie groups.
- No cohort has exact ties in `surprise_std` or in any response, so the
  1e-10 jitter does not resolve any exact tie here.
- The CPI-family surprises are, however, **near-discrete**: 9 distinct raw
  values, so the standardized surprises form tight clusters. The CPI-family
  responses are **heavy-tailed**: excess kurtosis about 3–6, with 31–48% of
  z-scored responses within ±0.25.
- The NFP surprise is continuous but extremely heavy-tailed: excess kurtosis
  about 60–70.
- This descriptive marginal diagnostic, which computed no MI, is
  **consistent with** the calibrated tie/atom and non-Gaussian-marginal
  sensitivity of KSG, and with the large positive SPY CPI-family null means.
  It does not prove the mechanism.

## N. NFP pointwise exception

All but one NFP KSG cell is non-significant pointwise. The single exception:

| Symbol | Family | k | h | KSG observed | Null mean | Pointwise p | Horizon-adjusted p |
|---|---|---|---|---|---|---|---|
| SPY | NFP | 10 | 60 min | 0.035518 bits | −0.025834 bits | **0.00869913** | **0.10058994** |

This **does not survive** the frozen horizon correction. The correct
interpretation is **no detectable NFP KSG dependence under the frozen
horizon-adjusted sensitivity procedure**. It is **not** correct to say that
every pointwise NFP test was non-significant. The 60-minute point is also
session-boundary-adjacent.

## O. Information-decay interpretation

Statistical information decay is **not** established by this analysis. GCMI
point estimates are highest at 1 minute, but KSG does not reproduce that
profile for CPI m/m (§J). No decay model, half-life, decay constant or
monotonic fit was estimated, and no shape test was pre-specified. The
results remain information profiles over horizon.

## P. Statistical limitations

- KSG is not the primary estimator. It has lower finite-sample power for
  monotonic dependence, tie and atom sensitivity, and sensitivity to
  non-Gaussian marginals.
- There are no calibrated KSG confidence intervals.
- No KSG Holm correction across panels was pre-specified.
- The D8 imbalance limits horizon-level interpretation (§K–§L).
- GCMI and KSG magnitudes are **not** comparable as equally calibrated
  quantities.
- **CPI and Core CPI are dependent panels**: shared releases and responses.
- **SPY and QQQ respond to overlapping releases**, so cross-symbol agreement
  is not independent replication.
- Synthetic calibration covered n ∈ {40, …, 120} and the scenarios of
  methodology §9. Real-data behaviour, such as the null-mean imbalance, can
  differ.
- This is retrospective historical inference. Forecasts are not
  independently verified as point-in-time.

## Q. Reproducibility and hashes

Outputs are in `data/reports/information_decay/ksg_sensitivity_v1/`,
combined sha256 **`c93846f4c6dd9d7c408120d762dbb91a38128080eada2c3fad5a88e7c36b1d6d`**.
Metadata is in `metadata/research/information_decay_ksg_sensitivity_v1.json`.

| File | sha256 |
|---|---|
| `cohort_verification.csv` | `84ec897ba62409d74d53b9f735e501a7f307fc0da5c5629fe7eacae07f7c0185` |
| `ksg_permutation_summary.csv` | `a041f7ec886b269f0614884f31ce36a6319555d555aa6ca27fc848878f6a4030` |
| `ksg_profile.csv` | `ff0d2347e3ae2aac0feb6ce5a41353f29da62d8237e43b10245b3c92feeba501` |
| `ksg_vs_gcmi.csv` | `d477232c6be936332ace3fe0c86079b289e4fe9da2425ee795640581adf8deaf` |
| `plot1_spy_ksg_vs_gcmi.svg` | `2d71d3693ef91b8c2c65e33704405c5fd1ca777c1bf14354d626828f63a9273c` |
| `plot2_qqq_ksg_vs_gcmi.svg` | `3e15714b20081ebbf1591d5f948a0698461067f8b39a9922cee810730945db7d` |
| `plot3_ksg_k_sensitivity.svg` | `c15eb851933dd8612f79984393fbf960dc0640f0f0083392bbaf9bc89b16fcf3` |
| `plot4_nfp_estimator_comparison.svg` | `cba1a8c8fc07580bbf93300937d0927bae731e3a1a35cf1c246c160653c1941f` |
| `summary.json` | `1bc865ecf9a181df91244a16a5c476e1911b7d4e0cdfdb85958cf4a4fc9a9f68` |

A rerun by the analyst reproduced every output byte for byte. The
independent review reproduced all 90 numerical cells, every permutation
p-value and all nine output files byte for byte.

## R. Independent review outcome

The independent review found:

- no scientific implementation defect;
- no frozen-methodology deviation;
- all 90 cells, all permutation p-values and all nine output files
  reproduced;
- 992 tests passing.

It required **one reporting-only fix**: correcting the agreement
classification (§H), documenting the D8 imbalance and its interpretation
limits (§K–§L), and recording the NFP pointwise exception (§N). This record
applies that fix. The results are frozen as tag `m3-ksg-sensitivity-v1`.
