"""Load the normalized interim macro files and run the calendar-anchored validation for a window.

Single definition of "the validated calendar releases" shared by scripts/validate_macro_events.py and
scripts/build_macro_market_response.py. Reads local parquet files only; never fetches."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from ..config import AppConfig, load_config
from ..event_mapping import EventMapping, load_event_mapping
from ..normalize.io import read_events
from .macro_events import (
    SCOPE_FAMILIES, ReconciliationResult, ValidationConfig, fred_profiles_from_config, reconcile,
)


def default_paths(config: AppConfig) -> Dict[str, Path]:
    macro = config.interim_root / "macro"
    return {"forex_factory": macro / "forex_factory_events.parquet", "mql5": macro / "mql5_events.parquet",
            "fred": macro / "fred_events.parquet"}


def run_macro_validation(start: dt.date, end: dt.date, *, config: Optional[AppConfig] = None,
                         mapping: Optional[EventMapping] = None, cfg: Optional[ValidationConfig] = None,
                         paths: Optional[Dict[str, Path]] = None,
                         families: Sequence[str] = SCOPE_FAMILIES,
                         timezone_evidence_window: Optional[Tuple[dt.date, dt.date]] = None
                         ) -> Tuple[ReconciliationResult, Dict[str, Path], Dict[str, int]]:
    config = config or load_config()
    mapping = mapping or load_event_mapping()
    paths = {**default_paths(config), **{k: Path(v) for k, v in (paths or {}).items() if v is not None}}
    cfg = cfg or ValidationConfig(families=tuple(families))
    events = {name: read_events(path) for name, path in paths.items()}
    profiles = fred_profiles_from_config(config)
    evidence = None
    if timezone_evidence_window is not None:
        evidence = reconcile(events["forex_factory"], events["mql5"], events["fred"], timezone_evidence_window[0],
                             timezone_evidence_window[1], mapping, cfg, profiles).timezone_validation
    result = reconcile(events["forex_factory"], events["mql5"], events["fred"], start, end, mapping, cfg, profiles,
                       timezone_evidence=evidence)
    result.timezone_evidence = evidence
    return result, paths, {k: len(v) for k, v in events.items()}
