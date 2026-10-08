#!/usr/bin/env python3
"""M3 information decay v1 -- KSG ESTIMATOR-SENSITIVITY analysis (secondary, exploratory).

    python scripts/run_information_decay_ksg_sensitivity_v1.py

Frozen protocol (nothing is tuned on real data):
  * KSG algorithm 1, max-norm, k in {3, 5, 10} fixed for every panel (methodology §8.2), via the
    frozen src/research/information_decay/ksg.py; each variable z-scored, seeded 1e-10 (z-units)
    jitter only to break exact ties; tie policy K-A = surprise_std as given (Amendment 01 D3);
    units bits. Jitter keys add symbol, response and estimator=ksg_k{k} (methodology §17).
  * The accepted raw cohorts only (SPY 93/93/103, QQQ 80/80/89), verified by release-ID hash
    against the frozen metadata; SPY and QQQ analysed separately, never pooled.
  * 10,000 stratified release-level permutations; permutation keys carry no estimator, so these
    are the SAME draws as the frozen GCMI runs; shared across horizons; raw-MI max-statistic (D8).
  * Sensitivity status (methodology §13.3; Amendment 02 §6): unadjusted and horizon-adjusted
    permutation p reported, no Holm, nothing confirmatory; no confidence interval (D7) -- point
    estimates, permutation null mean / SD, effective = observed - null mean (not truncated), k-spread.
  * Frozen GCMI results are READ (hash-verified) for comparison, never recomputed.
No residual KSG, no QQQ residual, no decay fitting.
Outputs: data/reports/information_decay/ksg_sensitivity_v1/ and
metadata/research/information_decay_ksg_sensitivity_v1.json.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.research.dataset import canonical_content_sha256  # noqa: E402
from src.research.information_decay import inference as inf  # noqa: E402
from src.research.information_decay import ksg  # noqa: E402
from src.research.information_decay.provenance import implementation_fingerprint  # noqa: E402
from src.research.information_decay.seeds import derive_seed, rng_for, seed_key  # noqa: E402

_spec = importlib.util.spec_from_file_location("primary_v1", REPO_ROOT / "scripts" / "run_information_decay_primary_v1.py")
prim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prim)

CONFIG = REPO_ROOT / "config" / "information_decay_ksg_sensitivity_v1.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "ksg_sensitivity_v1"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_ksg_sensitivity_v1.json"
FROZEN_K = (3, 5, 10)


def check_estimator_config(est: Dict[str, Any]) -> None:
    """No estimator reconfiguration: the frozen KSG settings only."""
    if tuple(est["k"]) != FROZEN_K:
        raise ValueError(f"KSG k must be the frozen {FROZEN_K}, got {est['k']}")
    if est["tie_policy"] != "K-A" or float(est["jitter_sd_z_units"]) != 1e-10 or est["units"] != "bits":
        raise ValueError("KSG tie policy / jitter / units differ from the frozen protocol")


def verify_frozen_cohort(c: pd.DataFrame, expected_n: int, frozen_sha: str, horizons: Sequence[int]) -> Dict[str, Any]:
    chk = prim.verify_cohort(c, str(c["event_family"].iloc[0]) if len(c) else "?", expected_n, horizons)
    if chk["release_ids_sha256"] != frozen_sha:
        chk["problems"].append("release IDs differ from the accepted frozen cohort")
    if c["symbol"].nunique() != 1:
        chk["problems"].append("more than one symbol in the panel (pooling)")
    return chk


def tie_counts(v: np.ndarray) -> int:
    """Number of observations that share their exact value with another observation."""
    _, counts = np.unique(v, return_counts=True)
    return int(counts[counts > 1].sum())


def ksg_panel(S: np.ndarray, Y: np.ndarray, perms: np.ndarray, k: int, jitter_rng: np.random.Generator,
              jitter_sd: float) -> Dict[str, Any]:
    """Frozen KSG (ksg.ksg_batch_bits) on z-scored, tie-jittered variables; observed and the
    permutation null for every horizon from ONE permutation set."""
    x = ksg.zscore(S) + jitter_sd * jitter_rng.standard_normal(len(S))
    Yz = ksg.zscore(Y, axis=0) + jitter_sd * jitter_rng.standard_normal(Y.shape)
    H = Y.shape[1]
    obs = np.empty(H)
    null = np.empty((perms.shape[0], H))
    for h in range(H):
        obs[h] = ksg.ksg_batch_bits(x[None, :], Yz[None, :, h], (k,))[0, 0]
        null[:, h] = ksg.ksg_batch_bits(x[perms], Yz[:, h], (k,))[:, 0]
    pv = inf.permutation_pvalues(obs, null)
    return {"obs": obs, "null_mean": pv["null_mean"], "null_sd": pv["null_sd"], "p_unadj": pv["p_unadjusted"],
            "p_adj": pv["p_horizon_adjusted"], "p_panel": pv["p_panel"], "null_q95": np.quantile(null, 0.95, axis=0)}


def agreement(gcmi_detect: bool, ksg_detect: Sequence[bool]) -> str:
    if gcmi_detect and all(ksg_detect):
        return "both detect (all k)"
    if gcmi_detect and any(ksg_detect):
        return "GCMI detects; KSG detects for some k"
    if gcmi_detect:
        return "GCMI only"
    if all(ksg_detect):
        return "KSG only (all k)"
    if any(ksg_detect):
        return "KSG only (some k)"
    return "neither"


MARKERS = {"o", "s", "^", "d"}


def svg_plot(path: Path, title: str, ylab: str, horizons: Sequence[int], series: List[Dict[str, Any]], note: str,
             boundary_h: int = 60) -> None:
    """Categorical-horizon marker plot; points are not connected (nothing interpolated)."""
    W, Hh, L, R, T, B = 820, 470, 80, 250, 50, 90
    vals = [v for s in series for v in s["y"]] + [0.0]
    ymin, ymax = min(vals), max(vals)
    pad = 0.08 * (ymax - ymin or 1.0)
    ymin, ymax = ymin - pad, ymax + pad
    step = (W - L - R) / len(horizons)
    sx = lambda i: L + (i + 0.5) * step  # noqa: E731
    sy = lambda v: T + (ymax - v) * (Hh - T - B) / (ymax - ymin)  # noqa: E731

    def marker(x, y, m, c):
        if m == "o":
            return f'<circle cx="{x}" cy="{y}" r="4.5" fill="{c}"/>'
        if m == "s":
            return f'<rect x="{x - 4}" y="{y - 4}" width="8" height="8" fill="none" stroke="{c}" stroke-width="1.5"/>'
        if m == "^":
            return f'<polygon points="{x},{y - 5} {x - 5},{y + 4} {x + 5},{y + 4}" fill="none" stroke="{c}" stroke-width="1.5"/>'
        return f'<polygon points="{x},{y - 5} {x + 5},{y} {x},{y + 5} {x - 5},{y}" fill="none" stroke="{c}" stroke-width="1.5"/>'
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" font-family="Helvetica,Arial,sans-serif" font-size="12">',
         f'<rect width="{W}" height="{Hh}" fill="white"/>', f'<text x="{L}" y="24" font-size="15" font-weight="bold">{title}</text>',
         f'<line x1="{L}" y1="{sy(0)}" x2="{W - R}" y2="{sy(0)}" stroke="#888" stroke-dasharray="4,3"/>',
         f'<line x1="{L}" y1="{T}" x2="{L}" y2="{Hh - B}" stroke="#333"/>']
    for kk in range(6):
        v = ymin + kk * (ymax - ymin) / 5
        o += [f'<text x="{L - 8}" y="{sy(v) + 4}" text-anchor="end">{v:.3f}</text>',
              f'<line x1="{L - 4}" y1="{sy(v)}" x2="{L}" y2="{sy(v)}" stroke="#333"/>']
    for i, h in enumerate(horizons):
        o.append(f'<text x="{sx(i)}" y="{Hh - B + 18}" text-anchor="middle">{h}m{" *" if h == boundary_h else ""}</text>')
    o.append(f'<text x="{(L + W - R) / 2}" y="{Hh - B + 38}" text-anchor="middle">horizon (categorical)</text>')
    o.append(f'<text transform="translate(18,{(T + Hh - B) / 2}) rotate(-90)" text-anchor="middle">{ylab}</text>')
    offs = np.linspace(-0.3, 0.3, len(series)) if len(series) > 1 else [0.0]
    for s, d in zip(series, offs):
        for i in range(len(horizons)):
            o.append(marker(sx(i) + d * step, sy(s["y"][i]), s.get("marker", "o"), s["color"]))
    for j, s in enumerate(series):
        y = T + 10 + 18 * j
        o.append(marker(W - R + 18, y, s.get("marker", "o"), s["color"]))
        o.append(f'<text x="{W - R + 30}" y="{y + 4}">{s["label"]}</text>')
    for kk, line in enumerate(note.split("\n")):
        o.append(f'<text x="{L}" y="{Hh - 24 + 13 * kk}" font-size="10" fill="#444">{line}</text>')
    o.append("</svg>")
    path.write_text("\n".join(o) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    cfg_bytes = CONFIG.read_bytes()
    cfg = yaml.safe_load(cfg_bytes)
    est = cfg["estimator"]
    check_estimator_config(est)
    H = list(cfg["horizons_min"])
    git = prim.git

    # ---------------- gates ----------------
    tc = lambda t: git("rev-parse", f"{t}^{{commit}}").stdout.strip()  # noqa: E731
    tags = {"calibration_freeze": tc(cfg["calibration_freeze"]["tag"])}
    tags.update({k: tc(v["tag"]) for k, v in cfg["frozen_results"].items()})
    expected = {"calibration_freeze": cfg["calibration_freeze"]["commit"], **{k: v["commit"] for k, v in cfg["frozen_results"].items()}}
    frozen = {}
    for k, v in cfg["frozen_results"].items():
        meta = json.loads((REPO_ROOT / v["metadata"]).read_text())
        d = REPO_ROOT / meta["outputs"]["directory"]
        frozen[k] = {"metadata_unchanged_since_tag": git("diff", "--quiet", tags[k], "--", v["metadata"]).returncode == 0,
                     "outputs_intact": all(prim.sha((d / f).read_bytes()) == h for f, h in meta["outputs"]["files_sha256"].items()),
                     "outputs_combined_sha256": meta["outputs"]["combined_sha256"], "meta": meta}
    status = git("status", "--porcelain").stdout.splitlines()
    tracked_dirty = [l for l in status if not l.startswith("??")]
    untracked = [l[3:] for l in status if l.startswith("??")]
    sci = {f["path"] for f in implementation_fingerprint(REPO_ROOT)["files"]}
    gates = {"head": git("rev-parse", "HEAD").stdout.strip(), "tags": tags,
             "tags_match_config": all(tags[k] == expected[k] for k in tags),
             "head_contains_tags": all(git("merge-base", "--is-ancestor", c, "HEAD").returncode == 0 for c in tags.values()),
             "frozen_library_identical_to_calibration_tag": git("diff", "--quiet", tags["calibration_freeze"], "--", prim.FROZEN_LIBRARY).returncode == 0
             and not git("status", "--porcelain", "--", prim.FROZEN_LIBRARY).stdout.strip(),
             "frozen_results": {k: {kk: vv for kk, vv in f.items() if kk != "meta"} for k, f in frozen.items()},
             "tracked_files_modified": tracked_dirty, "untracked_files": untracked,
             "untracked_scientific_files": [u for u in untracked if u in sci or u.startswith(prim.FROZEN_LIBRARY)]}
    protocol = {n: {"path": p, "commit": cfg["protocol_commits"][n],
                    "unchanged_since_commit": git("diff", "--quiet", cfg["protocol_commits"][n], "--", p).returncode == 0}
                for n, p in prim.PROTOCOL_DOCS.items()}
    ok = (gates["tags_match_config"] and gates["head_contains_tags"] and gates["frozen_library_identical_to_calibration_tag"]
          and all(f["metadata_unchanged_since_tag"] and f["outputs_intact"] for f in frozen.values())
          and not tracked_dirty and not gates["untracked_scientific_files"] and all(p["unchanged_since_commit"] for p in protocol.values()))
    if not ok:
        print(f"refusing to run: gate failed: {json.dumps(gates, indent=1, default=str)} {protocol}", file=sys.stderr)
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

    frozen_cohort_sha = {"SPY": {f: frozen["primary_spy"]["meta"]["cohorts"][f]["release_ids_sha256"] for f in cfg["families"]},
                         "QQQ": {f: frozen["qqq_replication"]["meta"]["cohorts"][f]["release_ids_sha256"] for f in cfg["families"]}}
    gprof = {}
    for sym, key in (("SPY", "primary_spy"), ("QQQ", "qqq_replication")):
        d = REPO_ROOT / frozen[key]["meta"]["outputs"]["directory"]
        for r in csv.DictReader(open(d / cfg["frozen_results"][key]["profile"])):
            gprof[(sym, r["family"], int(r["horizon_min"]))] = r

    # ---------------- cohorts ----------------
    cohorts, checks, ties = {}, {}, {}
    for sym in cfg["symbols"]:
        for fam in cfg["families"]:
            c = prim.build_cohort(df, fam, sym, H)
            checks[(sym, fam)] = verify_frozen_cohort(c, cfg["expected_n"][sym][fam], frozen_cohort_sha[sym][fam], H)
            cohorts[(sym, fam)] = c
            ties[(sym, fam)] = {"surprise_std_tied_obs": tie_counts(c["surprise_std"].astype(float).to_numpy()),
                                "surprise_raw_distinct_values": int(c["surprise_raw"].nunique()),
                                **{f"response_{h}m_tied_obs": tie_counts(c[f"post{h}m_ret"].astype(float).to_numpy()) for h in H}}
    if any(v["problems"] for v in checks.values()):
        print(f"STOP: cohort verification failed: {json.dumps({str(k): v for k, v in checks.items()}, indent=1, default=str)}", file=sys.stderr)
        return 1

    # ---------------- KSG panels ----------------
    B = int(cfg["permutations"])
    strat = f"stratification={cfg['strata']['policy_label']}"
    jsd = float(est["jitter_sd_z_units"])
    res, seeds = {}, {}
    for (sym, fam), c in cohorts.items():
        cid = checks[(sym, fam)]["cohort_id"]
        labels = prim.strata_labels(c, cfg["strata"]["order"], cfg["strata"]["min_stratum_size"])
        perm_parts = ("permutation", f"family={fam}", f"cohort={cid}", strat)   # identical to the frozen GCMI runs
        perms = inf.stratified_permutations(labels, B, rng_for(*perm_parts))
        S = c["surprise_std"].astype(float).to_numpy()
        Y = np.column_stack([c[f"post{h}m_ret"].astype(float).to_numpy() for h in H])
        seeds[(sym, fam)] = {"permutation": {"key": seed_key(*perm_parts), "seed": derive_seed(*perm_parts)}}
        for k in est["k"]:
            jparts = ("ksg_tie_jitter", f"family={fam}", f"cohort={cid}", strat, f"symbol={sym}", "response=raw", f"estimator=ksg_k{k}")
            res[(sym, fam, k)] = ksg_panel(S, Y, perms, int(k), rng_for(*jparts), jsd)
            seeds[(sym, fam)][f"jitter_k{k}"] = {"key": seed_key(*jparts), "seed": derive_seed(*jparts)}
            print(f"[{dt.datetime.now():%H:%M:%S}] {sym} {fam} k={k} done", file=sys.stderr, flush=True)

    # ---------------- tables ----------------
    thr = float(cfg["descriptive_detect_threshold"])
    prof, perm_summary, vs = [], [], []
    for (sym, fam, k), r in res.items():
        n = checks[(sym, fam)]["n"]
        for j, h in enumerate(H):
            prof.append({"symbol": sym, "family": fam, "horizon_min": h, "k": int(k), "n": n,
                         "session_label": "session-boundary-adjacent" if h == cfg["session_boundary_horizon_min"] else "premarket",
                         "ksg_observed_bits": float(r["obs"][j]), "null_mean_bits": float(r["null_mean"][j]),
                         "null_sd_bits": float(r["null_sd"][j]), "null_q95_bits": float(r["null_q95"][j]),
                         "effective_bits": float(r["obs"][j] - r["null_mean"][j]),
                         "p_raw": float(r["p_unadj"][j]), "p_horizon_adj": float(r["p_adj"][j]),
                         "inference_status": "sensitivity (no Holm, not confirmatory); no CI (D7)",
                         "cohort_id": checks[(sym, fam)]["cohort_id"], "B_perm": B})
        perm_summary.append({"symbol": sym, "family": fam, "k": int(k), "n": n, "panel_p_min_horizon_adj": float(r["p_panel"]),
                             "null_mean_range_bits": f"{float(r['null_mean'].min()):.6f}..{float(r['null_mean'].max()):.6f}",
                             "null_sd_range_bits": f"{float(r['null_sd'].min()):.6f}..{float(r['null_sd'].max()):.6f}",
                             "horizons_p_adj_le_threshold": json.dumps([h for h, p in zip(H, r["p_adj"]) if p <= thr]),
                             "max_statistic": cfg["max_statistic"]})
    pby = {(r["symbol"], r["family"], r["horizon_min"], r["k"]): r for r in prof}
    for sym in cfg["symbols"]:
        for fam in cfg["families"]:
            for h in H:
                g = gprof[(sym, fam, h)]
                g_det_flag = g.get("primary_detectable", g.get("replication_detectable"))
                row = {"symbol": sym, "family": fam, "horizon_min": h, "n": checks[(sym, fam)]["n"],
                       "gcmi_effective_bits_frozen": float(g["effective_bits"]), "gcmi_p_horizon_adj_frozen": float(g["p_horizon_adj"]),
                       "gcmi_family_corrected_detectable_frozen": g_det_flag == "True"}
                kd = []
                for k in est["k"]:
                    p = pby[(sym, fam, h, int(k))]
                    row[f"ksg_k{k}_effective_bits"] = p["effective_bits"]
                    row[f"ksg_k{k}_p_horizon_adj"] = p["p_horizon_adj"]
                    kd.append(p["p_horizon_adj"] <= thr)
                effs = [row[f"ksg_k{k}_effective_bits"] for k in est["k"]]
                row["ksg_k_spread_bits"] = max(effs) - min(effs)
                row["ksg_detect_count_of_3"] = sum(kd)
                row["agreement_descriptive"] = agreement(row["gcmi_p_horizon_adj_frozen"] <= thr, kd)
                row["note"] = "descriptive; estimators are not equally calibrated -- magnitudes are not compared"
                vs.append(row)

    # ---------------- outputs ----------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    W = prim.write_csv
    W(OUT_DIR / "ksg_profile.csv", prof)
    W(OUT_DIR / "ksg_permutation_summary.csv", perm_summary)
    W(OUT_DIR / "ksg_vs_gcmi.csv", vs)
    W(OUT_DIR / "cohort_verification.csv",
      [{"symbol": s, "family": f, "n": checks[(s, f)]["n"], "expected_n": checks[(s, f)]["expected_n"],
        "release_ids_sha256": checks[(s, f)]["release_ids_sha256"], "frozen_release_ids_sha256": frozen_cohort_sha[s][f],
        "matches_frozen": checks[(s, f)]["release_ids_sha256"] == frozen_cohort_sha[s][f], "cohort_id": checks[(s, f)]["cohort_id"],
        **{f"split_{k}": v for k, v in checks[(s, f)]["split_counts"].items()}, **ties[(s, f)]}
       for (s, f) in cohorts])
    note = "* 60m: 08:30 ET releases -- window ends at the 09:30 regular-session open (session-boundary-adjacent)."
    kcol = {3: "#7b3294", 5: "#c2a5cf", 10: "#008837"}
    kmk = {3: "s", 5: "^", 10: "d"}
    LBL, COL = prim.FAMILY_LABEL, prim.COLORS
    for sym, fname, title in (("SPY", "plot1_spy_ksg_vs_gcmi.svg", "SPY: GCMI (filled, frozen) vs KSG k=5 (open) effective bits"),
                              ("QQQ", "plot2_qqq_ksg_vs_gcmi.svg", "QQQ: GCMI (filled, frozen) vs KSG k=5 (open) effective bits")):
        svg_plot(OUT_DIR / fname, title, "effective (bits)", H,
                 [s for f in cfg["families"] for s in (
                     {"label": f"{LBL[f]} GCMI", "color": COL[f], "marker": "o",
                      "y": [v["gcmi_effective_bits_frozen"] for v in vs if v["symbol"] == sym and v["family"] == f]},
                     {"label": f"{LBL[f]} KSG k=5", "color": COL[f], "marker": "s",
                      "y": [v["ksg_k5_effective_bits"] for v in vs if v["symbol"] == sym and v["family"] == f]})],
                 note + "\nKSG is sensitivity-only and less calibrated than GCMI: magnitudes are not directly comparable. k=3/10 in plot3.")
    svg_plot(OUT_DIR / "plot3_ksg_k_sensitivity.svg", "KSG effective bits across k (SPY filled / QQQ open), CPI m/m and NFP",
             "KSG effective (bits)", H,
             [{"label": f"{sym} {LBL[f]} k={k}", "color": kcol[k], "marker": ("o" if sym == "SPY" else kmk[k]),
               "y": [pby[(sym, f, h, k)]["effective_bits"] for h in H]}
              for f in ("CPI_MOM", "NFP") for sym in ("SPY", "QQQ") for k in FROZEN_K],
             note + "\nUpper cluster: CPI m/m; near zero: NFP. Core CPI m/m is in ksg_profile.csv.")
    svg_plot(OUT_DIR / "plot4_nfp_estimator_comparison.svg", "NFP: GCMI (frozen) vs KSG k=3/5/10 effective bits",
             "effective (bits)", H,
             [s for sym in ("SPY", "QQQ") for s in (
                 [{"label": f"{sym} GCMI", "color": ("#3a9a4a" if sym == "SPY" else "#1f6fb4"), "marker": "o",
                   "y": [v["gcmi_effective_bits_frozen"] for v in vs if v["symbol"] == sym and v["family"] == "NFP"]}] +
                 [{"label": f"{sym} KSG k={k}", "color": ("#3a9a4a" if sym == "SPY" else "#1f6fb4"), "marker": kmk[k],
                   "y": [pby[(sym, "NFP", h, k)]["effective_bits"] for h in H]} for k in FROZEN_K])],
             note)
    summary = {"analysis": "ksg_sensitivity_v1", "status": "secondary exploratory estimator sensitivity; KSG is not a primary estimator",
               "real_market_mi_computed": True, "estimator": est, "inference_status": cfg["inference_status"],
               "confidence_intervals": cfg["confidence_intervals"], "max_statistic": cfg["max_statistic"],
               "cohorts": {f"{s}|{f}": {k: v for k, v in checks[(s, f)].items()} for (s, f) in cohorts},
               "tie_diagnostics": {f"{s}|{f}": ties[(s, f)] for (s, f) in cohorts},
               "seeds": {f"{s}|{f}": seeds[(s, f)] for (s, f) in cohorts},
               "permutation_draws_shared_with_frozen_gcmi": True,
               "frozen_gcmi_sources": {k: frozen[k]["outputs_combined_sha256"] for k in ("primary_spy", "qqq_replication")},
               "not_run": ["residual KSG", "QQQ residual", "KSG confidence intervals", "decay fitting", "CMI"]}
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {p.name: prim.sha(p.read_bytes()) for p in sorted(OUT_DIR.iterdir()) if p.is_file()}
    meta = {"dataset": "information_decay_ksg_sensitivity_v1", "secondary_exploratory": True, "real_market_mi_computed": True,
            "generated_at_utc": started.isoformat(timespec="seconds"), "calibration_freeze": cfg["calibration_freeze"],
            "frozen_results": cfg["frozen_results"], "protocol": protocol, "gates": gates, "implementation_fingerprint": impl,
            "config": {"path": str(CONFIG.relative_to(REPO_ROOT)), "sha256": prim.sha(cfg_bytes)},
            "input": {"event_response_path": ds["path"], "content_sha256": content, "rows": len(df),
                      "file_sha256": prim.sha((REPO_ROOT / ds["path"]).read_bytes()),
                      "market_dataset_fingerprint": meta_in["inputs"]["market_dataset"]["fingerprint"]},
            "cohorts": {f"{s}|{f}": {k: checks[(s, f)][k] for k in ("n", "release_ids_sha256", "cohort_id")} for (s, f) in cohorts},
            "outputs": {"directory": str(OUT_DIR.relative_to(REPO_ROOT)), "files_sha256": outputs,
                        "combined_sha256": prim.sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"cohorts": {f"{s}|{f}": checks[(s, f)]["n"] for (s, f) in cohorts},
                      "outputs_combined_sha256": meta["outputs"]["combined_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
