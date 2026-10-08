#!/usr/bin/env python3
"""M3 information decay v1 -- REPLICATION: QQQ raw information profile (secondary analysis).

    python scripts/run_information_decay_qqq_replication_v1.py

QQQ x {CPI m/m, Core CPI m/m, NFP} x h in {1, 5, 15, 30, 60} on QQQ's own cs_raw cohorts, with the
frozen primary procedure: GCMI tie policy B ("permutation-null-adjusted Gaussian-copula dependence
in bits"), 10,000 stratified release permutations shared across horizons, horizon max-T, a
"QQQ replication family correction" (Holm over the three QQQ panels; never pooled with SPY),
2,000-replicate stratified percentile bootstrap (nominal 95%, synthetic coverage ~0.93).

Comparisons (descriptive only):
  * frozen SPY primary result -- READ from its hash-verified outputs, never recomputed;
  * matched-cohort sensitivity on the exact SPY ∩ QQQ release intersection: QQQ-on-intersection and
    SPY-on-intersection (same release IDs, shared permutation / bootstrap-index draws);
  * SPY / QQQ cohort overlap.
No residual, Null B, KSG, CMI or decay fitting. QQQ and SPY observations are never pooled: every
estimated panel contains a single symbol.
Outputs: data/reports/information_decay/qqq_replication_v1/ and
metadata/research/information_decay_qqq_replication_v1.json.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.research.dataset import canonical_content_sha256  # noqa: E402
from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay import gcmi  # noqa: E402
from src.research.information_decay import inference as inf  # noqa: E402
from src.research.information_decay.benchmarks import gaussian_mi_bits  # noqa: E402
from src.research.information_decay.provenance import implementation_fingerprint  # noqa: E402
from src.research.information_decay.seeds import derive_seed, rng_for, seed_key  # noqa: E402

_spec = importlib.util.spec_from_file_location("primary_v1", REPO_ROOT / "scripts" / "run_information_decay_primary_v1.py")
prim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prim)

CONFIG = REPO_ROOT / "config" / "information_decay_qqq_replication_v1.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "qqq_replication_v1"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_qqq_replication_v1.json"


def single_symbol(c: pd.DataFrame) -> str:
    """No pooling: an estimated panel must contain exactly one symbol."""
    syms = c["symbol"].unique()
    if len(syms) != 1:
        raise ValueError(f"panel pools symbols {sorted(syms)}; SPY and QQQ must never be pooled")
    return str(syms[0])


def analyse_panel_symbol(S, raw, Y, labels, family: str, cid: str, policy: str, policy_label: str,
                         est_key: str, B: int, B_boot: int, symbol: str) -> Dict[str, Any]:
    """The frozen primary panel analysis (scripts/run_information_decay_primary_v1.analyse_panel)
    with the symbol in the bootstrap tie-break key instead of the hard-coded "SPY" (methodology
    §17: tie-break keys add symbol). Permutation and bootstrap-index keys contain no symbol, so two
    symbols on the same cohort share those draws (§11.4). Bit-identical to analyse_panel for SPY."""
    strat = f"stratification={policy_label}"
    perm_parts = ("permutation", f"family={family}", f"cohort={cid}", strat)
    boot_parts = ("bootstrap", f"family={family}", f"cohort={cid}", strat)
    tie_parts = boot_parts + (f"symbol={symbol}", "response=raw", f"estimator={est_key}")
    perms = inf.stratified_permutations(labels, B, rng_for(*perm_parts))
    zs = cal.tie_policy_scores(S, raw, policy, None)
    zy = np.column_stack([gcmi.copnorm(Y[:, j]) for j in range(Y.shape[1])])
    obs = gcmi.gcmi_from_scores(zs[None, :], zy.T[None, :, :])[0]
    null = gcmi.gcmi_permutation_matrix(zs, zy, perms)
    pv = inf.permutation_pvalues(obs, null)
    bench = {}
    for name, xs, ys in (("pearson", S, Y),
                         ("spearman", gcmi.average_ranks(S), np.column_stack([gcmi.average_ranks(Y[:, j]) for j in range(Y.shape[1])]))):
        xc = xs - xs.mean()
        yc = ys - ys.mean(axis=0)
        den = np.sqrt((xc * xc).sum() * (yc * yc).sum(axis=0))
        r = (xc @ yc) / den
        with np.errstate(all="ignore"):
            rn = np.abs(xc[perms] @ yc / den)
        bench[name] = {"r": r, "p_two_sided": (1 + (rn >= np.abs(r)[None, :]).sum(axis=0)) / (B + 1)}
    bidx = inf.stratified_bootstrap(labels, B_boot, rng_for(*boot_parts))
    tb = rng_for(*tie_parts)
    boot = cal._rowwise_gcmi_batch(S[bidx], Y[bidx], lambda X: cal._random_tiebreak_scores(X, tb),
                                   s_scorer=lambda X: cal.tie_policy_scores_bootstrap(X, raw[bidx], policy, tb))
    lo, hi = np.quantile(boot, 0.025, axis=0), np.quantile(boot, 0.975, axis=0)
    seeds = {k: {"key": seed_key(*parts), "seed": derive_seed(*parts)}
             for k, parts in (("permutation", perm_parts), ("bootstrap_indices", boot_parts), ("bootstrap_tiebreak", tie_parts))}
    return {"obs": obs, "null_mean": pv["null_mean"], "null_sd": pv["null_sd"], "p_unadj": pv["p_unadjusted"],
            "p_adj": pv["p_horizon_adjusted"], "p_panel": pv["p_panel"], "boot_raw_lo": lo, "boot_raw_hi": hi,
            "boot_mean": boot.mean(axis=0), "bench": bench, "seeds": seeds,
            "ilin": gaussian_mi_bits(bench["pearson"]["r"]), "ince_bias_bits": gcmi.ince_bias_bits(len(S)),
            "strata_layout": {int(s): int((labels == s).sum()) for s in np.unique(labels)}}


def run_panel(c: pd.DataFrame, family: str, cfg: Dict[str, Any], H: Sequence[int]) -> Dict[str, Any]:
    symbol = single_symbol(c)
    labels = prim.strata_labels(c, cfg["strata"]["order"], cfg["strata"]["min_stratum_size"])
    est = cfg["estimator"]
    return analyse_panel_symbol(c["surprise_std"].astype(float).to_numpy(), c["surprise_raw"].astype(float).to_numpy(),
                                np.column_stack([c[f"post{h}m_ret"].astype(float).to_numpy() for h in H]), labels, family,
                                prim.cohort_id(c["release_id"]), est["tie_policy"], cfg["strata"]["policy_label"], est["key"],
                                int(cfg["permutations"]), int(cfg["bootstrap"]["replicates"]), symbol)


def exclusion_reasons(df: pd.DataFrame, family: str, symbol: str, horizons: Sequence[int]) -> Dict[str, int]:
    """Why each release of family x symbol is or is not in cs_raw (first failing rule)."""
    from src.research import filters
    x = df[(df["event_family"] == family) & (df["symbol"] == symbol)]
    usable_s = filters.usable_std_surprise(x)
    usable_r = {h: filters.usable_response(x, f"post{h}m") for h in horizons}
    out: Counter = Counter()
    for i in x.index:
        r = x.loc[i]
        if r["event_status"] != "usable":
            out[f"event_excluded:{r['event_status']}"] += 1
        elif not usable_s.loc[i]:
            out[f"surprise:{r['surprise_std_status']}"] += 1
        elif bool(pd.notna(r["provisional_market_data"]) and r["provisional_market_data"]):
            out["provisional_market_data"] += 1
        else:
            bad = [h for h in horizons if not usable_r[h].loc[i]]
            if bad:
                out[f"window:{r[f'post{bad[0]}m_status']}@{bad[0]}m(first_failing)"] += 1
            else:
                out["included"] += 1
    return dict(sorted(out.items()))


def overlap(spy_ids: Sequence[str], qqq_ids: Sequence[str]) -> Dict[str, Any]:
    s, q = set(spy_ids), set(qqq_ids)
    return {"spy_n": len(s), "qqq_n": len(q), "intersection": len(s & q), "spy_only": len(s - q), "qqq_only": len(q - s),
            "spy_only_ids": sorted(s - q), "qqq_only_ids": sorted(q - s)}


def intersection_frames(spy_c: pd.DataFrame, qqq_c: pd.DataFrame):
    ids = set(spy_c["release_id"]) & set(qqq_c["release_id"])
    s = spy_c[spy_c["release_id"].isin(ids)].reset_index(drop=True)
    q = qqq_c[qqq_c["release_id"].isin(ids)].reset_index(drop=True)
    if list(s["release_id"]) != list(q["release_id"]):
        raise RuntimeError("SPY and QQQ intersection frames are not in identical release order")
    return s, q


def profile_rows(r: Dict[str, Any], family: str, symbol: str, cohort_kind: str, cid: str, n: int, H, cfg) -> List[Dict[str, Any]]:
    rows = []
    for j, h in enumerate(H):
        rows.append({"family": family, "symbol": symbol, "response_type": "raw", "cohort_kind": cohort_kind, "cohort_id": cid,
                     "estimator": "gcmi", "tie_policy": cfg["estimator"]["tie_policy"], "horizon_min": h,
                     "session_label": "session-boundary-adjacent" if h == cfg["session_boundary_horizon_min"] else "premarket",
                     "n": n, "gcmi_raw_bits": float(r["obs"][j]), "null_mean_bits": float(r["null_mean"][j]),
                     "null_sd_bits": float(r["null_sd"][j]), "effective_bits": float(r["obs"][j] - r["null_mean"][j]),
                     "p_raw": float(r["p_unadj"][j]), "p_horizon_adj": float(r["p_adj"][j]), "panel_p": float(r["p_panel"]),
                     "bootstrap_low": float(r["boot_raw_lo"][j] - r["null_mean"][j]),
                     "bootstrap_high": float(r["boot_raw_hi"][j] - r["null_mean"][j]),
                     "bootstrap_raw_low": float(r["boot_raw_lo"][j]), "bootstrap_raw_high": float(r["boot_raw_hi"][j]),
                     "pearson": float(r["bench"]["pearson"]["r"][j]), "spearman": float(r["bench"]["spearman"]["r"][j]),
                     "p_pearson": float(r["bench"]["pearson"]["p_two_sided"][j]),
                     "p_spearman": float(r["bench"]["spearman"]["p_two_sided"][j]), "i_lin_bits": float(r["ilin"][j]),
                     "ci_label": "nominal 95% percentile bootstrap interval of effective GCMI (synthetic coverage ~0.93)",
                     "B_perm": int(cfg["permutations"]), "B_boot": int(cfg["bootstrap"]["replicates"]),
                     "perm_seed": r["seeds"]["permutation"]["seed"], "boot_seed": r["seeds"]["bootstrap_indices"]["seed"],
                     "tiebreak_seed": r["seeds"]["bootstrap_tiebreak"]["seed"]})
    return rows


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    cfg_bytes = CONFIG.read_bytes()
    cfg = yaml.safe_load(cfg_bytes)
    H = list(cfg["horizons_min"])
    git = prim.git

    # ---------------- gates ----------------
    def tag_commit(t):
        return git("rev-parse", f"{t}^{{commit}}").stdout.strip()
    tags = {k: tag_commit(cfg[k]["tag"]) for k in ("calibration_freeze", "primary_result", "residual_result")}
    gates = {"head": git("rev-parse", "HEAD").stdout.strip(), "tags": tags,
             "tags_match_config": all(tags[k] == cfg[k]["commit"] for k in tags),
             "head_contains_tags": all(git("merge-base", "--is-ancestor", c, "HEAD").returncode == 0 for c in tags.values()),
             "frozen_library_identical_to_calibration_tag": git("diff", "--quiet", tags["calibration_freeze"], "--", prim.FROZEN_LIBRARY).returncode == 0
             and not git("status", "--porcelain", "--", prim.FROZEN_LIBRARY).stdout.strip()}
    frozen = {}
    for k in ("primary_result", "residual_result"):
        mp = cfg[k]["metadata"]
        meta = json.loads((REPO_ROOT / mp).read_text())
        d = REPO_ROOT / meta["outputs"]["directory"]
        frozen[k] = {"metadata_unchanged_since_tag": git("diff", "--quiet", tags[k], "--", mp).returncode == 0,
                     "outputs_intact": all(prim.sha((d / f).read_bytes()) == h for f, h in meta["outputs"]["files_sha256"].items()),
                     "outputs_combined_sha256": meta["outputs"]["combined_sha256"], "meta": meta}
    status = git("status", "--porcelain").stdout.splitlines()
    tracked_dirty = [l for l in status if not l.startswith("??")]
    untracked = [l[3:] for l in status if l.startswith("??")]
    sci = {f["path"] for f in implementation_fingerprint(REPO_ROOT)["files"]}
    gates.update(frozen_spy_results={k: {kk: v for kk, v in f.items() if kk != "meta"} for k, f in frozen.items()},
                 tracked_files_modified=tracked_dirty, untracked_files=untracked,
                 untracked_scientific_files=[u for u in untracked if u in sci or u.startswith(prim.FROZEN_LIBRARY)])
    protocol = {n: {"path": p, "commit": cfg["protocol_commits"][n],
                    "unchanged_since_commit": git("diff", "--quiet", cfg["protocol_commits"][n], "--", p).returncode == 0}
                for n, p in prim.PROTOCOL_DOCS.items()}
    ok = (gates["tags_match_config"] and gates["head_contains_tags"] and gates["frozen_library_identical_to_calibration_tag"]
          and all(f["metadata_unchanged_since_tag"] and f["outputs_intact"] for f in frozen.values())
          and not tracked_dirty and not gates["untracked_scientific_files"] and all(p["unchanged_since_commit"] for p in protocol.values()))
    if not ok:
        print(f"refusing to run: gate failed: {json.dumps({k: v for k, v in gates.items()}, indent=1, default=str)} {protocol}", file=sys.stderr)
        return 1

    meta_in = json.loads((REPO_ROOT / cfg["input"]["research_metadata"]).read_text())
    ds = meta_in["outputs"]["event_response"]
    df = pd.read_parquet(REPO_ROOT / ds["path"])
    content = canonical_content_sha256(df, ds["hash_key"])
    if content != ds["content_sha256"] or len(df) != ds["rows"]:
        print("refusing to run: dataset content hash mismatch", file=sys.stderr)
        return 1
    started = dt.datetime.now(dt.timezone.utc)
    impl = implementation_fingerprint(REPO_ROOT)
    pmeta = frozen["primary_result"]["meta"]
    spy_frozen_rows = list(csv.DictReader(open(REPO_ROOT / pmeta["outputs"]["directory"] / "primary_profile.csv")))
    spy_frozen = {(r["family"], int(r["horizon_min"])): r for r in spy_frozen_rows}

    # ---------------- cohorts ----------------
    sym, csym = cfg["symbol"], cfg["comparison_symbol"]
    cohorts, checks, spy_cohorts, ov, excl = {}, {}, {}, {}, {}
    for fam in cfg["families"]:
        q = prim.build_cohort(df, fam, sym, H)
        s = prim.build_cohort(df, fam, csym, H)
        checks[fam] = prim.verify_cohort(q, fam, cfg["expected_n"][fam], H)
        spy_chk = prim.verify_cohort(s, fam, cfg["spy_primary_n"][fam], H)
        if spy_chk["release_ids_sha256"] != pmeta["cohorts"][fam]["release_ids_sha256"]:
            checks[fam]["problems"].append("SPY cohort rebuilt here differs from the frozen SPY primary cohort")
        checks[fam]["problems"] += [f"SPY: {p}" for p in spy_chk["problems"]]
        cohorts[fam], spy_cohorts[fam] = q, s
        ov[fam] = overlap(list(s["release_id"]), list(q["release_id"]))
        excl[fam] = {"QQQ": exclusion_reasons(df, fam, sym, H), "SPY": exclusion_reasons(df, fam, csym, H)}
    if any(v["problems"] for v in checks.values()):
        print(f"STOP: cohort verification failed: {json.dumps(checks, indent=1, default=str)}", file=sys.stderr)
        return 1

    # ---------------- QQQ replication panels + matched intersection sensitivity ----------------
    res, mres = {}, {}
    rows, mrows, comp, mcomp = [], [], [], []
    for fam in cfg["families"]:
        q = cohorts[fam]
        res[fam] = run_panel(q, fam, cfg, H)
        rows += profile_rows(res[fam], fam, sym, "cs_raw", checks[fam]["cohort_id"], len(q), H, cfg)
        si, qi = intersection_frames(spy_cohorts[fam], q)
        icid = prim.cohort_id(si["release_id"])
        mres[fam] = {"SPY": run_panel(si, fam, cfg, H), "QQQ": run_panel(qi, fam, cfg, H), "cohort_id": icid, "n": len(si),
                     "release_ids_sha256": prim.sha("\n".join(sorted(si["release_id"])).encode())}
        for s_ in ("QQQ", "SPY"):
            mrows += profile_rows(mres[fam][s_], fam, s_, "spy_qqq_intersection", icid, len(si), H, cfg)
    holm = prim.holm_closed_testing({f: res[f]["p_panel"] for f in cfg["replication_holm_family"]},
                                    {f: {"horizons": H, "p": list(res[f]["p_adj"])} for f in cfg["replication_holm_family"]},
                                    float(cfg["alpha"]))
    for r in rows:
        r.update(replication_holm_adjusted_p=holm[r["family"]]["holm_adjusted_p"],
                 panel_rejected_replication_holm=holm[r["family"]]["panel_rejected"],
                 replication_detectable=r["horizon_min"] in holm[r["family"]]["detectable_horizons"])
    by = {(r["family"], r["horizon_min"]): r for r in rows}
    mby = {(r["family"], r["symbol"], r["horizon_min"]): r for r in mrows}
    for fam in cfg["families"]:
        for h in H:
            qr, sf = by[(fam, h)], spy_frozen[(fam, h)]
            comp.append({"family": fam, "horizon_min": h, "spy_n": int(sf["n"]), "qqq_n": qr["n"],
                         "spy_frozen_effective": float(sf["effective_bits"]), "qqq_effective": qr["effective_bits"],
                         "difference_qqq_minus_spy": qr["effective_bits"] - float(sf["effective_bits"]),
                         "spy_frozen_p_adj": float(sf["p_horizon_adj"]), "qqq_p_adj": qr["p_horizon_adj"],
                         "spy_frozen_boot_low": float(sf["bootstrap_low"]), "spy_frozen_boot_high": float(sf["bootstrap_high"]),
                         "qqq_boot_low": qr["bootstrap_low"], "qqq_boot_high": qr["bootstrap_high"],
                         "note": "descriptive only; different cohorts; no SPY-vs-QQQ difference test was pre-specified"})
            ms, mq = mby[(fam, "SPY", h)], mby[(fam, "QQQ", h)]
            mcomp.append({"family": fam, "horizon_min": h, "n": ms["n"], "cohort_id": ms["cohort_id"],
                          "spy_on_intersection_effective": ms["effective_bits"], "qqq_on_intersection_effective": mq["effective_bits"],
                          "difference_qqq_minus_spy": mq["effective_bits"] - ms["effective_bits"],
                          "spy_boot_low": ms["bootstrap_low"], "spy_boot_high": ms["bootstrap_high"],
                          "qqq_boot_low": mq["bootstrap_low"], "qqq_boot_high": mq["bootstrap_high"],
                          "spy_p_adj_descriptive": ms["p_horizon_adj"], "qqq_p_adj_descriptive": mq["p_horizon_adj"],
                          "note": "descriptive matched-cohort sensitivity; exact SPY∩QQQ release IDs; no family-level decision"})

    # ---------------- outputs ----------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    W = prim.write_csv
    W(OUT_DIR / "qqq_profile.csv", [dict(r, spy_frozen_effective=float(spy_frozen[(r["family"], r["horizon_min"])]["effective_bits"]),
                                         spy_matched_effective=mby[(r["family"], "SPY", r["horizon_min"])]["effective_bits"],
                                         spy_matched_n=mby[(r["family"], "SPY", r["horizon_min"])]["n"]) for r in rows])
    W(OUT_DIR / "qqq_family_holm.csv", [dict(family=f, correction="QQQ replication family correction (Holm, 3 QQQ panels; not pooled with SPY)",
                                             **{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in holm[f].items()})
                                        for f in cfg["replication_holm_family"]])
    W(OUT_DIR / "spy_qqq_comparison.csv", comp)
    W(OUT_DIR / "spy_matched_qqq_cohort.csv", mrows)
    W(OUT_DIR / "spy_matched_qqq_comparison.csv", mcomp)
    W(OUT_DIR / "cohort_release_ids.csv",
      [{"family": f, "cohort": c, "release_id": rid} for f in cfg["families"]
       for c, frame in (("QQQ_cs_raw", cohorts[f]), ("SPY_QQQ_intersection", intersection_frames(spy_cohorts[f], cohorts[f])[0]))
       for rid in frame["release_id"]])
    W(OUT_DIR / "cohort_overlap.csv", [{"family": f, **{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in ov[f].items()}}
                                       for f in cfg["families"]])
    note = "* 60m: 08:30 ET releases -- window ends at the 09:30 regular-session open (session-boundary-adjacent)."
    LBL, COL = prim.FAMILY_LABEL, prim.COLORS
    prim.svg_points_plot(OUT_DIR / "plot1_qqq_information_profile.svg", "QQQ replication: effective GCMI (bits) by horizon",
                         "effective GCMI (bits)", H,
                         [{"label": LBL[f], "color": COL[f], "y": [r["effective_bits"] for r in rows if r["family"] == f],
                           "lo": [r["bootstrap_low"] for r in rows if r["family"] == f],
                           "hi": [r["bootstrap_high"] for r in rows if r["family"] == f]} for f in cfg["families"]],
                         cfg["session_boundary_horizon_min"], note + "\nBars: nominal 95% percentile bootstrap interval (synthetic coverage ~0.93, not 0.95). QQQ n = 80 / 80 / 89.")
    prim.svg_points_plot(OUT_DIR / "plot2_spy_vs_qqq.svg", "Frozen SPY primary (filled) vs QQQ replication (open): effective GCMI",
                         "effective GCMI (bits)", H,
                         [s for f in cfg["families"] for s in (
                             {"label": f"{LBL[f]} SPY (n={cfg['spy_primary_n'][f]})", "color": COL[f],
                              "y": [c["spy_frozen_effective"] for c in comp if c["family"] == f]},
                             {"label": f"{LBL[f]} QQQ (n={cfg['expected_n'][f]})", "color": COL[f], "marker": "s",
                              "y": [c["qqq_effective"] for c in comp if c["family"] == f]})],
                         cfg["session_boundary_horizon_min"], note + "\nDifferent cohorts: descriptive only; no SPY-vs-QQQ test was pre-specified.")
    prim.svg_points_plot(OUT_DIR / "plot3_spy_matched_vs_qqq.svg", "Same releases (SPY ∩ QQQ): SPY (filled) vs QQQ (open) effective GCMI",
                         "effective GCMI (bits)", H,
                         [s for f in cfg["families"] for s in (
                             {"label": f"{LBL[f]} SPY (n={mres[f]['n']})", "color": COL[f],
                              "y": [c["spy_on_intersection_effective"] for c in mcomp if c["family"] == f]},
                             {"label": f"{LBL[f]} QQQ (n={mres[f]['n']})", "color": COL[f], "marker": "s",
                              "y": [c["qqq_on_intersection_effective"] for c in mcomp if c["family"] == f]})],
                         cfg["session_boundary_horizon_min"], note + "\nExact same release IDs for both assets; descriptive matched-cohort sensitivity.")
    prim.svg_points_plot(OUT_DIR / "plot4_qqq_correlation_benchmarks.svg", "QQQ: Pearson (filled) / Spearman (open) benchmarks",
                         "correlation", H,
                         [s for f in cfg["families"] for s in (
                             {"label": f"{LBL[f]} Pearson", "color": COL[f], "y": [r["pearson"] for r in rows if r["family"] == f]},
                             {"label": f"{LBL[f]} Spearman", "color": COL[f], "marker": "s",
                              "y": [r["spearman"] for r in rows if r["family"] == f]})],
                         cfg["session_boundary_horizon_min"], note)
    summary = {"analysis": "qqq_replication_v1", "replication": True, "real_market_mi_computed": True,
               "estimator_description": "permutation-null-adjusted Gaussian-copula dependence in bits (primarily monotonic)",
               "cohorts": {f: {k: v for k, v in checks[f].items()} for f in cfg["families"]},
               "exclusion_reasons": excl, "overlap": ov,
               "matched_intersection": {f: {"n": mres[f]["n"], "cohort_id": mres[f]["cohort_id"],
                                            "release_ids_sha256": mres[f]["release_ids_sha256"]} for f in cfg["families"]},
               "replication_holm": holm, "seeds": {f: res[f]["seeds"] for f in cfg["families"]},
               "matched_seeds": {f: {s_: mres[f][s_]["seeds"] for s_ in ("SPY", "QQQ")} for f in cfg["families"]},
               "strata_layout": {f: res[f]["strata_layout"] for f in cfg["families"]},
               "spy_frozen_source": {"outputs_combined_sha256": frozen["primary_result"]["outputs_combined_sha256"],
                                     "recomputed": False},
               "not_run": ["QQQ residual", "Null B on QQQ", "KSG", "histogram MI", "CMI", "decay fitting"]}
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {p.name: prim.sha(p.read_bytes()) for p in sorted(OUT_DIR.iterdir()) if p.is_file()}
    meta = {"dataset": "information_decay_qqq_replication_v1", "replication": True, "real_market_mi_computed": True,
            "generated_at_utc": started.isoformat(timespec="seconds"),
            "calibration_freeze": cfg["calibration_freeze"], "primary_result": cfg["primary_result"],
            "residual_result": cfg["residual_result"], "protocol": protocol, "gates": gates,
            "implementation_fingerprint": impl, "config": {"path": str(CONFIG.relative_to(REPO_ROOT)), "sha256": prim.sha(cfg_bytes)},
            "input": {"event_response_path": ds["path"], "content_sha256": content, "rows": len(df),
                      "file_sha256": prim.sha((REPO_ROOT / ds["path"]).read_bytes()),
                      "market_dataset_fingerprint": meta_in["inputs"]["market_dataset"]["fingerprint"]},
            "frozen_spy_outputs_combined_sha256": {k: f["outputs_combined_sha256"] for k, f in frozen.items()},
            "cohorts": {f: {k: checks[f][k] for k in ("n", "release_ids_sha256", "cohort_id", "split_counts")} for f in cfg["families"]},
            "matched_intersection": {f: {"n": mres[f]["n"], "release_ids_sha256": mres[f]["release_ids_sha256"]} for f in cfg["families"]},
            "outputs": {"directory": str(OUT_DIR.relative_to(REPO_ROOT)), "files_sha256": outputs,
                        "combined_sha256": prim.sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"qqq_cohorts": {f: checks[f]["n"] for f in checks},
                      "intersection": {f: mres[f]["n"] for f in mres},
                      "outputs_combined_sha256": meta["outputs"]["combined_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
