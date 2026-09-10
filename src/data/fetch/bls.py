"""BLS (Bureau of Labor Statistics) client -- v0 STUB.

Per architecture scope ("do not over-engineer the official-series
integration in v0; start with the indicators we actually need"), FRED
(fetch/fred.py) already covers the official validation series this
project needs first (CPI, NFP, unemployment rate, average hourly
earnings all have FRED equivalents). BLS is therefore not implemented
yet -- this module exists so the provider slot in
config/data_sources.yaml and the CLI wiring are real, and so a future
implementation has an obvious place to land.

Real endpoint for when this is implemented:
POST https://api.bls.gov/publicAPI/v2/timeseries/data/
Body: {"seriesid": [...], "startyear": "...", "endyear": "...", "registrationkey": "..."}
Constraint to handle then: v2 unregistered access is limited to 10 years
per request and 25 series; chunk by decade using dates.iter_date_chunks
(frequency="year", grouped into <=10-year windows) the same way the
other fetchers chunk internally.
"""
from __future__ import annotations

import datetime as dt

from ..config import AppConfig
from ..manifest import Manifest


def fetch_bls_series(
    config: AppConfig,
    manifest: Manifest,
    series_id: str,
    start_date: dt.date,
    end_date: dt.date,
    force: bool = False,
) -> None:
    raise NotImplementedError(
        "BLS ingestion is not implemented in v0. FRED covers the currently "
        "required official validation series (config/data_sources.yaml "
        "providers.fred.series). Implement here when a BLS-specific series "
        "is actually needed."
    )
