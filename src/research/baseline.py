"""Expected-response baseline (config `baseline`): an auditable, strictly causal benchmark.

  expected_post{h}m_ret(row) = mean of post{h}m_ret over EARLIER usable rows of the same
                               (symbol, event_family, surprise_sign) group
  resid_post{h}m_ret         = post{h}m_ret - expected_post{h}m_ret

EVALUATION PARADIGM: walk-forward / online expanding (`baseline_evaluation_paradigm`). Each row's
expectation uses every strictly earlier usable release of its group -- including earlier
validation- or test-period releases. It is NOT a model frozen on the development split and its
residuals are not a standard holdout evaluation.

"Earlier" is a semantic UTC timestamp comparison (resolution-safe); members of the same release
never inform each other. Eligibility comes from the central filters (src/research/filters.py).
Per row: history count and `baseline_post{h}m_training_cutoff_utc` (latest release used, always
before the row itself).
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from .filters import usable_response
from .surprise import utc_timestamps


def baseline_id(spec: Dict[str, Any]) -> str:
    b = spec["baseline"]
    return f"{b['name']}_v{b['version']}"


def add_baseline(wide: pd.DataFrame, spec: Dict[str, Any]) -> pd.DataFrame:
    b = spec["baseline"]
    min_hist = int(b["min_history"])
    out = wide.copy().reset_index(drop=True)
    out["release_timestamp_utc"] = utc_timestamps(out["release_timestamp_utc"])
    out["baseline_model"] = baseline_id(spec)
    out["baseline_evaluation_paradigm"] = b["evaluation_paradigm"]
    sign = out["surprise_sign"].map(lambda x: "none" if pd.isna(x) else str(int(x)))
    for h in b["horizons"]:
        exp, res, n_hist, cut, st = ([None] * len(out) for _ in range(5))
        ok_all = usable_response(out, f"post{h}m")
        for (_sym, _fam, sg), idx in out.groupby([out["symbol"], out["event_family"], sign]).groups.items():
            sub = out.loc[idx]
            ok = ok_all.loc[idx]
            h_ts = sub.loc[ok, "release_timestamp_utc"]
            h_val = sub.loc[ok, f"post{h}m_ret"].astype(float)
            for i in idx:
                t = out.at[i, "release_timestamp_utc"]
                if sg == "none" or pd.isna(t):
                    st[i] = "no_surprise_sign"
                    continue
                before = h_ts < t  # semantic, strictly earlier
                n = int(before.sum())
                n_hist[i] = n
                if n:
                    cut[i] = h_ts[before].max().isoformat()
                if n < min_hist:
                    st[i] = "insufficient_history"
                    continue
                e = float(h_val[before].mean())
                exp[i] = e
                if ok_all.at[i]:
                    res[i] = float(out.at[i, f"post{h}m_ret"]) - e
                    st[i] = "ok"
                else:
                    st[i] = "no_observed_response"
        out[f"expected_post{h}m_ret"] = pd.array(exp, dtype="Float64")
        out[f"resid_post{h}m_ret"] = pd.array(res, dtype="Float64")
        out[f"baseline_post{h}m_n_history"] = pd.array(n_hist, dtype="Int64")
        out[f"baseline_post{h}m_training_cutoff_utc"] = cut
        out[f"baseline_post{h}m_status"] = st
    return out


def assign_split(release_date_et, spec: Dict[str, Any]) -> str:
    if release_date_et is None or (isinstance(release_date_et, float) and np.isnan(release_date_et)):
        return "unassigned"
    s = spec["split"]
    for name in ("development", "validation", "test"):
        lo, hi = s[name]
        if lo <= release_date_et <= hi:
            return name
    return "outside_research_range"
