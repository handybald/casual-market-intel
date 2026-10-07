#!/usr/bin/env python3
"""M3 information decay v1 -- PRIMARY real-data information profiles (SPY raw response).

    python scripts/run_information_decay_primary_v1.py

The FIRST real-data MI computation of the project. Scope (frozen protocol; nothing is chosen here):
  SPY x {CPI m/m, Core CPI m/m, NFP} x h in {1, 5, 15, 30, 60}, common-support cohort cs_raw,
  GCMI with tie policy B ("permutation-null-adjusted Gaussian-copula dependence in bits"),
  stratified release-level permutation (10,000 + observed, shared across horizons), horizon max-T,
  Holm across the three family panels, stratified percentile bootstrap (2,000; nominal 95%,
  synthetic coverage ~0.93), Pearson / Spearman benchmarks. No QQQ, residual or KSG analysis.

Gates (refuses to run otherwise):
  * HEAD contains the calibration-freeze tag and the frozen library
    (src/research/information_decay) is byte-identical to the tagged state;
  * no tracked file is modified and no scientific file is untracked (other untracked files are
    recorded in the provenance);
  * methodology and Amendments 02-04 are unchanged since their commits;
  * the event-response dataset content hash equals its recorded value;
  * every family's cs_raw cohort has exactly the frozen size, one row per release, the same
    releases at every horizon.

This runner only CALLS the frozen estimator / inference library; it changes no scientific behaviour.
Outputs: data/reports/information_decay/primary_v1/ and metadata/research/information_decay_primary_v1.json.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.research import filters  # noqa: E402
from src.research.dataset import canonical_content_sha256  # noqa: E402
from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay import gcmi  # noqa: E402
from src.research.information_decay import inference as inf  # noqa: E402
from src.research.information_decay.benchmarks import gaussian_mi_bits  # noqa: E402
from src.research.information_decay.provenance import implementation_fingerprint  # noqa: E402
from src.research.information_decay.seeds import derive_seed, rng_for, seed_key  # noqa: E402

CONFIG = REPO_ROOT / "config" / "information_decay_primary_v1.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "primary_v1"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_primary_v1.json"
FROZEN_LIBRARY = "src/research/information_decay"
PROTOCOL_DOCS = {"methodology": "docs/research/information_decay_v1_methodology.md",
                 "amendment_02": "docs/research/information_decay_v1_amendment_02.md",
                 "amendment_03": "docs/research/information_decay_v1_amendment_03.md",
                 "amendment_04": "docs/research/information_decay_v1_amendment_04.md"}
FAMILY_LABEL = {"CPI_MOM": "CPI m/m", "CORE_CPI_MOM": "Core CPI m/m", "NFP": "NFP"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=REPO_ROOT, capture_output=True, text=True)


# ---------------------------------------------------------------------------------------------
# cohort
# ---------------------------------------------------------------------------------------------
def build_cohort(df: pd.DataFrame, family: str, symbol: str, horizons: Sequence[int]) -> pd.DataFrame:
    """cs_raw (methodology §7.1 rules 1-4) via the frozen central filters, sorted chronologically
    (release timestamp, then release_id) so permutation positions depend on releases, not rows."""
    x = df[(df["event_family"] == family) & (df["symbol"] == symbol)]
    ok = filters.usable_std_surprise(x) & ~x["provisional_market_data"].fillna(False).astype(bool)
    for h in horizons:
        ok &= filters.usable_response(x, f"post{h}m")
    c = x[ok].copy()
    c["release_timestamp_utc"] = pd.to_datetime(c["release_timestamp_utc"], utc=True)
    return c.sort_values(["release_timestamp_utc", "release_id"]).reset_index(drop=True)


def cohort_id(release_ids: Sequence[str]) -> str:
    return sha("\n".join(sorted(str(r) for r in release_ids)).encode())[:16]


def verify_cohort(c: pd.DataFrame, family: str, expected_n: int, horizons: Sequence[int]) -> Dict[str, Any]:
    problems = []
    if len(c) != expected_n:
        problems.append(f"n = {len(c)} differs from frozen n = {expected_n}")
    if c["release_id"].isna().any() or not c["release_id"].is_unique:
        problems.append("release_id missing or not unique (pseudo-duplication)")
    if c["event_family"].nunique() != 1:
        problems.append("more than one family in the panel")
    for h in horizons:
        if c[f"post{h}m_ret"].isna().any():
            problems.append(f"missing response at {h}m: horizon-specific cohort drift")
    if c["surprise_std"].isna().any() or c["surprise_raw"].isna().any():
        problems.append("missing surprise")
    return {"family": family, "n": int(len(c)), "expected_n": int(expected_n), "problems": problems,
            "release_ids_sha256": sha("\n".join(sorted(map(str, c["release_id"]))).encode()),
            "cohort_id": cohort_id(c["release_id"]),
            "split_counts": {k: int(v) for k, v in c["split"].value_counts().sort_index().items()}}


def strata_labels(c: pd.DataFrame, order: Sequence[str], min_size: int) -> np.ndarray:
    pos = {name: i for i, name in enumerate(order)}
    if not c["split"].isin(order).all():
        raise RuntimeError(f"release outside the frozen strata: {sorted(set(c['split']) - set(order))}")
    return inf.merge_small_strata(c["split"].map(pos).to_numpy(), min_size)


# ---------------------------------------------------------------------------------------------
# family panel analysis (frozen library calls only)
# ---------------------------------------------------------------------------------------------
def analyse_panel(S: np.ndarray, raw: np.ndarray, Y: np.ndarray, labels: np.ndarray, family: str, cid: str,
                  policy: str, policy_label: str, est_key: str, B: int, B_boot: int) -> Dict[str, Any]:
    strat = f"stratification={policy_label}"
    perm_parts = ("permutation", f"family={family}", f"cohort={cid}", strat)
    boot_parts = ("bootstrap", f"family={family}", f"cohort={cid}", strat)
    tie_parts = boot_parts + ("symbol=SPY", "response=raw", f"estimator={est_key}")
    perms = inf.stratified_permutations(labels, B, rng_for(*perm_parts))
    zs = cal.tie_policy_scores(S, raw, policy, None)
    zy = np.column_stack([gcmi.copnorm(Y[:, j]) for j in range(Y.shape[1])])
    obs = gcmi.gcmi_from_scores(zs[None, :], zy.T[None, :, :])[0]
    null = gcmi.gcmi_permutation_matrix(zs, zy, perms)
    pv = inf.permutation_pvalues(obs, null)
    # Pearson / Spearman benchmarks on the same permutations (two-sided, statistic |r|)
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
    # stratified release-level percentile bootstrap (frozen D5 procedure)
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


def holm_closed_testing(panel_p: Dict[str, float], adj_p: Dict[str, Sequence[float]], alpha: float) -> Dict[str, Any]:
    """Methodology §13.2: Holm across panels on P_j = min_h p_adj; a horizon h of panel j shows
    detectable dependence iff panel j is rejected and p_adj(j,h) <= the Holm threshold at which j
    was rejected."""
    order = sorted(panel_p, key=lambda k: (panel_p[k], k))
    m = len(order)
    out, stop = {}, False
    for i, fam in enumerate(order):
        thr = alpha / (m - i)
        rej = (not stop) and panel_p[fam] <= thr
        if not rej:
            stop = True
        out[fam] = {"panel_p": panel_p[fam], "holm_rank": i + 1, "holm_threshold": thr,
                    "holm_adjusted_p": min(1.0, max((m - j) * panel_p[order[j]] for j in range(i + 1))),
                    "panel_rejected": rej,
                    "detectable_horizons": [int(h) for h, p in zip(adj_p[fam]["horizons"], adj_p[fam]["p"]) if rej and p <= thr]}
    return out


# ---------------------------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------------------------
def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    cols: List[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else (repr(r[c]) if isinstance(r[c], float) else r[c]) for c in cols])
    path.write_text(buf.getvalue(), encoding="utf-8")


COLORS = {"CPI_MOM": "#1f6fb4", "CORE_CPI_MOM": "#d9772b", "NFP": "#3a9a4a"}


def svg_points_plot(path: Path, title: str, ylab: str, horizons: Sequence[int],
                    series: List[Dict[str, Any]], boundary_h: int, note: str) -> None:
    """Categorical-horizon point plot (markers + optional intervals). Points are NOT connected, so
    nothing is interpolated between horizons and no monotone shape is implied."""
    W, H, L, R, T, B = 760, 460, 80, 190, 50, 90
    vals = [v for s in series for v in list(s["y"]) + list(s.get("lo", [])) + list(s.get("hi", []))]
    ymin, ymax = min(vals + [0.0]), max(vals + [0.0])
    pad = 0.08 * (ymax - ymin or 1.0)
    ymin, ymax = ymin - pad, ymax + pad
    sx = lambda i: L + (i + 0.5) * (W - L - R) / len(horizons)  # noqa: E731
    sy = lambda v: T + (ymax - v) * (H - T - B) / (ymax - ymin)  # noqa: E731
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Helvetica,Arial,sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<text x="{L}" y="24" font-size="15" font-weight="bold">{title}</text>',
         f'<line x1="{L}" y1="{sy(0)}" x2="{W - R}" y2="{sy(0)}" stroke="#888" stroke-dasharray="4,3"/>',
         f'<line x1="{L}" y1="{T}" x2="{L}" y2="{H - B}" stroke="#333"/>']
    for k in range(6):
        v = ymin + k * (ymax - ymin) / 5
        o.append(f'<text x="{L - 8}" y="{sy(v) + 4}" text-anchor="end">{v:.3f}</text>')
        o.append(f'<line x1="{L - 4}" y1="{sy(v)}" x2="{L}" y2="{sy(v)}" stroke="#333"/>')
    for i, h in enumerate(horizons):
        lab = f"{h}m" + (" *" if h == boundary_h else "")
        o.append(f'<text x="{sx(i)}" y="{H - B + 18}" text-anchor="middle">{lab}</text>')
    o.append(f'<text x="{(L + W - R) / 2}" y="{H - B + 38}" text-anchor="middle">horizon (categorical)</text>')
    o.append(f'<text transform="translate(18,{(T + H - B) / 2}) rotate(-90)" text-anchor="middle">{ylab}</text>')
    off = np.linspace(-0.22, 0.22, len(series)) if len(series) > 1 else [0.0]
    step = (W - L - R) / len(horizons)
    for s, d in zip(series, off):
        for i in range(len(horizons)):
            x = sx(i) + d * step
            if "lo" in s:
                o.append(f'<line x1="{x}" y1="{sy(s["lo"][i])}" x2="{x}" y2="{sy(s["hi"][i])}" stroke="{s["color"]}" stroke-width="1.5"/>')
            mk = (f'<circle cx="{x}" cy="{sy(s["y"][i])}" r="4.5" fill="{s["color"]}"/>' if s.get("marker", "o") == "o"
                  else f'<rect x="{x - 4}" y="{sy(s["y"][i]) - 4}" width="8" height="8" fill="none" stroke="{s["color"]}" stroke-width="1.5"/>')
            o.append(mk)
    for j, s in enumerate(series):
        y = T + 10 + 20 * j
        o.append(f'<circle cx="{W - R + 18}" cy="{y}" r="4.5" fill="{s["color"]}"/>' if s.get("marker", "o") == "o" else
                 f'<rect x="{W - R + 14}" y="{y - 4}" width="8" height="8" fill="none" stroke="{s["color"]}" stroke-width="1.5"/>')
        o.append(f'<text x="{W - R + 30}" y="{y + 4}">{s["label"]}</text>')
    for k, line in enumerate(note.split("\n")):   # footnote lines (caveats must stay fully visible)
        o.append(f'<text x="{L}" y="{H - 24 + 13 * k}" font-size="10" fill="#444">{line}</text>')
    o.append("</svg>")
    path.write_text("\n".join(o) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    cfg_bytes = CONFIG.read_bytes()
    cfg = yaml.safe_load(cfg_bytes)

    # ---------------- reproducibility gates ----------------
    tag = cfg["calibration_freeze"]["tag"]
    tag_commit = git("rev-parse", f"{tag}^{{commit}}").stdout.strip()
    head = git("rev-parse", "HEAD").stdout.strip()
    gates = {"tag_commit": tag_commit, "head": head,
             "tag_matches_config": tag_commit == cfg["calibration_freeze"]["commit"],
             "head_contains_tag": git("merge-base", "--is-ancestor", tag_commit, "HEAD").returncode == 0,
             "frozen_library_identical_to_tag": git("diff", "--quiet", tag_commit, "--", FROZEN_LIBRARY).returncode == 0
             and not git("status", "--porcelain", "--", FROZEN_LIBRARY).stdout.strip()}
    status = git("status", "--porcelain").stdout.splitlines()
    tracked_dirty = [l for l in status if not l.startswith("??")]
    untracked = [l[3:] for l in status if l.startswith("??")]
    sci = {f["path"] for f in implementation_fingerprint(REPO_ROOT)["files"]}
    untracked_scientific = [u for u in untracked if u in sci or u.startswith(FROZEN_LIBRARY)]
    gates.update(tracked_files_modified=tracked_dirty, untracked_files=untracked,
                 untracked_scientific_files=untracked_scientific)
    protocol = {}
    for name, path in PROTOCOL_DOCS.items():
        commit = cfg["protocol_commits"][name]
        protocol[name] = {"path": path, "commit": commit,
                          "unchanged_since_commit": git("diff", "--quiet", commit, "--", path).returncode == 0}
    ok = (gates["tag_matches_config"] and gates["head_contains_tag"] and gates["frozen_library_identical_to_tag"]
          and not tracked_dirty and not untracked_scientific and all(p["unchanged_since_commit"] for p in protocol.values()))
    if not ok:
        print(f"refusing to run: reproducibility gate failed: {json.dumps(gates, indent=1)} {protocol}", file=sys.stderr)
        return 1

    # ---------------- input dataset ----------------
    meta_in = json.loads((REPO_ROOT / cfg["input"]["research_metadata"]).read_text())
    ds = meta_in["outputs"]["event_response"]
    ds_path = REPO_ROOT / ds["path"]
    df = pd.read_parquet(ds_path)
    content = canonical_content_sha256(df, ds["hash_key"])
    if content != ds["content_sha256"] or len(df) != ds["rows"]:
        print(f"refusing to run: dataset content hash {content} != recorded {ds['content_sha256']}", file=sys.stderr)
        return 1
    started = dt.datetime.now(dt.timezone.utc)
    impl = implementation_fingerprint(REPO_ROOT)
    H = list(cfg["horizons_min"])

    # ---------------- cohorts ----------------
    cohorts, checks = {}, {}
    for fam in cfg["families"]:
        c = build_cohort(df, fam, cfg["symbol"], H)
        checks[fam] = verify_cohort(c, fam, cfg["expected_n"][fam], H)
        cohorts[fam] = c
    if any(v["problems"] for v in checks.values()):
        print(f"STOP: cohort verification failed: {json.dumps(checks, indent=1)}", file=sys.stderr)
        return 1

    # ---------------- panels ----------------
    est = cfg["estimator"]
    res = {}
    for fam in cfg["families"]:
        c = cohorts[fam]
        labels = strata_labels(c, cfg["strata"]["order"], cfg["strata"]["min_stratum_size"])
        res[fam] = analyse_panel(c["surprise_std"].astype(float).to_numpy(), c["surprise_raw"].astype(float).to_numpy(),
                                 np.column_stack([c[f"post{h}m_ret"].astype(float).to_numpy() for h in H]), labels,
                                 fam, checks[fam]["cohort_id"], est["tie_policy"], cfg["strata"]["policy_label"],
                                 est["key"], int(cfg["permutations"]), int(cfg["bootstrap"]["replicates"]))
    holm = holm_closed_testing({f: res[f]["p_panel"] for f in cfg["holm_family"]},
                               {f: {"horizons": H, "p": list(res[f]["p_adj"])} for f in cfg["holm_family"]},
                               float(cfg["alpha"]))

    # ---------------- outputs ----------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for fam in cfg["families"]:
        r = res[fam]
        for j, h in enumerate(H):
            rows.append({"family": fam, "symbol": cfg["symbol"], "response_type": "raw", "cohort_kind": "cs_raw",
                         "cohort_id": checks[fam]["cohort_id"], "estimator": "gcmi", "tie_policy": est["tie_policy"],
                         "horizon_min": h,
                         "session_label": "session-boundary-adjacent" if h == cfg["session_boundary_horizon_min"] else "premarket",
                         "n": checks[fam]["n"],
                         "gcmi_raw_bits": float(r["obs"][j]), "null_mean_bits": float(r["null_mean"][j]),
                         "null_sd_bits": float(r["null_sd"][j]),
                         "effective_bits": float(r["obs"][j] - r["null_mean"][j]),
                         "pearson": float(r["bench"]["pearson"]["r"][j]), "spearman": float(r["bench"]["spearman"]["r"][j]),
                         "p_pearson": float(r["bench"]["pearson"]["p_two_sided"][j]),
                         "p_spearman": float(r["bench"]["spearman"]["p_two_sided"][j]),
                         "i_lin_bits": float(r["ilin"][j]),
                         "p_raw": float(r["p_unadj"][j]), "p_horizon_adj": float(r["p_adj"][j]),
                         "bootstrap_low": float(r["boot_raw_lo"][j] - r["null_mean"][j]),
                         "bootstrap_high": float(r["boot_raw_hi"][j] - r["null_mean"][j]),
                         "bootstrap_raw_low": float(r["boot_raw_lo"][j]), "bootstrap_raw_high": float(r["boot_raw_hi"][j]),
                         "bootstrap_mean_raw_bits": float(r["boot_mean"][j]),
                         "ci_method": "stratified percentile bootstrap, shifted by the full-sample null mean (effective-MI interval)",
                         "ci_label": "nominal 95% percentile bootstrap interval (synthetic coverage ~0.93)",
                         "ince_bias_bits_diagnostic": float(r["ince_bias_bits"]),
                         "panel_p_holm_input": float(r["p_panel"]),
                         "holm_adjusted_p": float(holm[fam]["holm_adjusted_p"]),
                         "panel_rejected_holm": holm[fam]["panel_rejected"],
                         "primary_detectable": h in holm[fam]["detectable_horizons"],
                         "B_perm": int(cfg["permutations"]), "B_boot": int(cfg["bootstrap"]["replicates"]),
                         "perm_seed": r["seeds"]["permutation"]["seed"], "boot_seed": r["seeds"]["bootstrap_indices"]["seed"],
                         "strata_layout": json.dumps(r["strata_layout"], sort_keys=True)})
    write_csv(OUT_DIR / "primary_profile.csv", rows)
    write_csv(OUT_DIR / "holm_family.csv", [dict(family=f, **{k: (json.dumps(v) if isinstance(v, list) else v)
                                                               for k, v in holm[f].items()}) for f in cfg["holm_family"]])
    cohort_rows = [{"family": f, "release_id": rid, "release_timestamp_utc": ts.isoformat(), "split": sp}
                   for f in cfg["families"] for rid, ts, sp in zip(cohorts[f]["release_id"], cohorts[f]["release_timestamp_utc"], cohorts[f]["split"])]
    write_csv(OUT_DIR / "cohort_release_ids.csv", cohort_rows)
    note = "* 60m: 08:30 ET releases -- window ends at the 09:30 regular-session open (session-boundary-adjacent)."
    svg_points_plot(OUT_DIR / "plot1_effective_gcmi.svg", "SPY primary: effective GCMI (bits) by horizon",
                    "effective GCMI (bits)", H,
                    [{"label": FAMILY_LABEL[f], "color": COLORS[f],
                      "y": [x["effective_bits"] for x in rows if x["family"] == f],
                      "lo": [x["bootstrap_low"] for x in rows if x["family"] == f],
                      "hi": [x["bootstrap_high"] for x in rows if x["family"] == f]} for f in cfg["families"]],
                    cfg["session_boundary_horizon_min"], note + "\nBars: nominal 95% percentile bootstrap interval of effective GCMI (synthetic coverage ~0.93, not 0.95).")
    svg_points_plot(OUT_DIR / "plot2_raw_gcmi_and_null.svg", "SPY primary: raw GCMI (filled) and permutation-null mean (open)",
                    "bits", H,
                    [s for f in cfg["families"] for s in (
                        {"label": f"{FAMILY_LABEL[f]} raw", "color": COLORS[f], "y": [x["gcmi_raw_bits"] for x in rows if x["family"] == f]},
                        {"label": f"{FAMILY_LABEL[f]} null", "color": COLORS[f], "marker": "s",
                         "y": [x["null_mean_bits"] for x in rows if x["family"] == f]})],
                    cfg["session_boundary_horizon_min"], note)
    svg_points_plot(OUT_DIR / "plot3_correlation_benchmarks.svg", "SPY primary: Pearson (filled) / Spearman (open) benchmarks",
                    "correlation", H,
                    [s for f in cfg["families"] for s in (
                        {"label": f"{FAMILY_LABEL[f]} Pearson", "color": COLORS[f], "y": [x["pearson"] for x in rows if x["family"] == f]},
                        {"label": f"{FAMILY_LABEL[f]} Spearman", "color": COLORS[f], "marker": "s",
                         "y": [x["spearman"] for x in rows if x["family"] == f]})],
                    cfg["session_boundary_horizon_min"], note)
    summary = {"analysis": "primary_v1", "real_market_mi_computed": True, "scope": "SPY raw cs_raw, GCMI policy B",
               "estimator_description": "permutation-null-adjusted Gaussian-copula dependence in bits (primarily monotonic)",
               "cohorts": checks, "holm": holm,
               "seeds": {f: res[f]["seeds"] for f in cfg["families"]},
               "strata_layout": {f: res[f]["strata_layout"] for f in cfg["families"]},
               "not_run": ["QQQ replication", "residual MI", "KSG sensitivity"]}
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {p.name: sha(p.read_bytes()) for p in sorted(OUT_DIR.iterdir()) if p.is_file()}
    meta = {"dataset": "information_decay_primary_v1", "real_market_mi_computed": True,
            "generated_at_utc": started.isoformat(timespec="seconds"),
            "calibration_freeze": {"tag": tag, "commit": tag_commit}, "protocol": protocol, "gates": gates,
            "implementation_fingerprint": impl, "config": {"path": str(CONFIG.relative_to(REPO_ROOT)), "sha256": sha(cfg_bytes)},
            "input": {"event_response_path": ds["path"], "content_sha256": content, "rows": len(df),
                      "file_sha256": sha(ds_path.read_bytes()),
                      "market_dataset_fingerprint": meta_in["inputs"]["market_dataset"]["fingerprint"],
                      "research_metadata_sha256": sha((REPO_ROOT / cfg["input"]["research_metadata"]).read_bytes())},
            "cohorts": {f: {k: checks[f][k] for k in ("n", "release_ids_sha256", "cohort_id", "split_counts")} for f in cfg["families"]},
            "outputs": {"directory": str(OUT_DIR.relative_to(REPO_ROOT)), "files_sha256": outputs,
                        "combined_sha256": sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"cohorts": {f: checks[f]["n"] for f in checks}, "outputs_combined_sha256": meta["outputs"]["combined_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
