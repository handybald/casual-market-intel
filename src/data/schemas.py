"""Canonical, provider-independent data models.

These are the types everything downstream (normalize/, validation/,
and eventually the PyTorch/causal-inference stages) is built against.
Raw fetch modules produce provider-native rows; normalize/ modules
convert those into these schemas.
"""
from __future__ import annotations

import datetime as dt
import enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MacroSource(str, enum.Enum):
    MQL5 = "MQL5"
    FOREX_FACTORY = "FOREX_FACTORY"
    FRED = "FRED"
    BLS = "BLS"


UNCERTAIN_TIMEZONES = {"UNKNOWN", "SERVER"}


class ValidationStatus(str, enum.Enum):
    MATCH = "MATCH"
    ROUNDING_DIFFERENCE = "ROUNDING_DIFFERENCE"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"


class MacroEvent(BaseModel):
    """Canonical representation of a single macro calendar release.

    `provider_forecast` is explicitly NOT called `economist_consensus`:
    Forex Factory does not publish enough methodology to claim it is a
    formally defined survey median/mean. It is stored as a provider
    forecast, with its source recorded, for diagnostics/features -- not
    as ground truth.
    """

    model_config = ConfigDict(use_enum_values=True)

    event_id: str

    event_family: str  # canonical key from config/event_mapping.yaml, e.g. "NFP"
    indicator: str  # human-readable canonical name, e.g. "Nonfarm Payrolls"
    release_bundle: Optional[str] = None  # e.g. "EMPLOYMENT", "CPI"

    release_timestamp_utc: dt.datetime
    release_timestamp_ny: Optional[dt.datetime] = None

    actual: Optional[float] = None

    provider_forecast: Optional[float] = None
    forecast_source: Optional[str] = None

    previous: Optional[float] = None
    revised_previous: Optional[float] = None

    official_actual: Optional[float] = None
    official_source: Optional[str] = None
    official_vintage_date: Optional[dt.date] = None

    unit: Optional[str] = None
    importance: Optional[str] = None

    # -- provenance ------------------------------------------------------
    source: MacroSource
    source_event_id: Optional[str] = None
    source_timestamp: Optional[dt.datetime] = None  # original, un-normalized
    source_timezone: Optional[str] = None  # e.g. "America/New_York", "UNKNOWN"
    source_url: Optional[str] = None
    retrieval_timestamp_utc: dt.datetime

    @property
    def timestamp_is_trustworthy(self) -> bool:
        """False when we could not confidently establish the source
        timezone (e.g. unresolved or broker/server-local, like MQL5's
        "SERVER" tag) -- downstream code should treat release_timestamp_utc
        as provisional in that case rather than silently trusting it."""
        return self.source_timezone is not None and self.source_timezone not in UNCERTAIN_TIMEZONES


class MarketBar(BaseModel):
    """Canonical single OHLCV bar (post-normalization)."""

    symbol: str
    timestamp_utc: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    transactions: Optional[int] = None

    source: str = "MASSIVE"
    retrieval_timestamp_utc: dt.datetime


class MacroValidationResult(BaseModel):
    event_family: str
    release_timestamp_utc: dt.datetime
    field: str  # which field was compared, e.g. "actual"
    status: ValidationStatus
    values: dict  # {source_name: value}
    note: Optional[str] = None
