"""Amendment 04 exact primary-cohort-size qualification (docs/research/information_decay_v1_amendment_04.md).

The cell inventory is DERIVED from the frozen qualification definitions -- required (gate, policy)
pairs of the Amendment-03 config x hard-family cells of the Amendment-02 config x exact sizes --
and must equal the committed inventory file before anything runs. Each cell is then classified by
the unchanged Amendment-03 fixed-sample rule (amendment03.classify_cell). Synthetic only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

PRIMARY_EXACT_SIZES = (93, 103)   # SPY cs_raw: CPI m/m & Core CPI m/m = 93, NFP = 103 (methodology §7.2)
POLICY_ESTIMATOR = {"A": "gcmi", "B": "gcmi_B", "C": "gcmi_C"}
INVENTORY_FIELDS = ("gate", "cell", "scenario", "mode", "estimator", "tie_policy", "procedure", "n",
                    "replicates", "replicate_indices", "seed_identity")


def derive_exact_inventory(base_cfg: Dict[str, Any], a02: Dict[str, Any], a03: Dict[str, Any],
                           sizes: Sequence[int] = PRIMARY_EXACT_SIZES) -> List[Dict[str, Any]]:
    cells = {c["id"]: c for c in list(base_cfg["cells"]) + list(a02["new_cells"])}
    required = {(x["gate"], x["policy"]) for x in a03["required"]}
    n_final = int(a03["n_final"])
    inv = []
    for spec in a02["hard_family"]:
        for pol in spec["policies"]:
            if (spec["gate"], pol) not in required:
                continue
            c = cells[spec["cell"]]
            for n in sizes:
                inv.append({"gate": spec["gate"], "cell": spec["cell"], "scenario": c["scenario"],
                            "mode": c.get("mode", "A"), "estimator": POLICY_ESTIMATOR[pol], "tie_policy": pol,
                            "procedure": "strat", "n": int(n), "replicates": n_final,
                            "replicate_indices": [0, n_final - 1],
                            "seed_identity": f"m3_information_decay|v1|calibration|{spec['cell']}|{n}|<rep>|<purpose>"})
    return inv


def inventories_equal(derived: List[Dict[str, Any]], committed: List[Dict[str, Any]]) -> bool:
    key = lambda d: tuple(str(d[f]) for f in INVENTORY_FIELDS)  # noqa: E731
    return sorted(map(key, derived)) == sorted(map(key, committed))
