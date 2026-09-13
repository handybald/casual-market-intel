"""Best-effort reference-period inference for monthly macro calendar releases.

CPI, Core CPI, Nonfarm Payrolls, Unemployment Rate, and Average Hourly
Earnings (everything currently in config/event_mapping.yaml) are all
conventionally released describing the PRIOR calendar month -- e.g.
January CPI is released in February. This is a well-established
reporting convention for exactly these indicators, NOT a field either
MQL5 or Forex Factory actually publishes: it is an inferred heuristic,
not verified per-event, and does NOT generalize to quarterly data or
same-month releases.

Used only so MQL5/Forex Factory rows get a `reference_period` and CAN
be joined against FRED-derived official rows (which have a real
reference_period taken from the observation date) -- see
validation/macro.py's grouping logic. If a new event family with a
different release convention is added, this heuristic must be revisited.
"""
from __future__ import annotations

import datetime as dt


def infer_prior_month_reference_period(release_date: dt.date) -> dt.date:
    if release_date.month == 1:
        return dt.date(release_date.year - 1, 12, 1)
    return dt.date(release_date.year, release_date.month - 1, 1)
