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


class TimestampQuality(str, enum.Enum):
    """How much to trust `release_timestamp_utc`.

    CONFIRMED  -- converted using a timezone the operator has explicitly
                  verified for this deployment (e.g. a confirmed MQL5
                  broker server timezone).
    ASSUMED    -- converted using a *documented default* that has not
                  been independently verified (e.g. Forex Factory's
                  anonymous-session display timezone). Usable, but
                  callers doing anything precision-sensitive should know
                  it's an assumption, not a confirmed fact.
    TENTATIVE  -- the source itself only gave an imprecise time (e.g.
                  "All Day" / "Tentative" on Forex Factory) -- there is
                  no single instant to convert, regardless of timezone.
    UNRESOLVED -- no usable timezone information at all. `release_timestamp_utc`
                  is None. These rows MUST be excluded from any
                  minute-level join against market data.
    """

    CONFIRMED = "CONFIRMED"
    ASSUMED = "ASSUMED"
    TENTATIVE = "TENTATIVE"
    UNRESOLVED = "UNRESOLVED"


class ValueUnit(str, enum.Enum):
    """Unit a parsed macro value is expressed in, after normalization.

    THOUSANDS is the canonical unit for count-style indicators (payrolls
    etc) -- Forex Factory's raw K/M/B suffixes are converted into it so
    "2.5M" and "2500K" compare equal instead of silently diverging.
    """

    PERCENT = "PERCENT"
    THOUSANDS = "THOUSANDS"
    LEVEL = "LEVEL"
    UNKNOWN = "UNKNOWN"


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

    # The calendar period this value DESCRIBES (e.g. 2024-01-01 for
    # "January 2024 CPI"), distinct from when it was released. Populated
    # reliably only for FRED-derived official rows (the observation
    # date); left None for MQL5/Forex Factory rows where we don't have a
    # reliable source field for it (see normalize/*.py docstrings).
    reference_period: Optional[dt.date] = None

    # Nullable: an unresolved/untrustworthy timestamp is represented as
    # None (quarantined), never silently defaulted to a guessed UTC
    # instant. See `timestamp_quality`.
    release_timestamp_utc: Optional[dt.datetime] = None
    release_timestamp_ny: Optional[dt.datetime] = None
    timestamp_quality: TimestampQuality = TimestampQuality.UNRESOLVED

    actual: Optional[float] = None
    actual_unit: ValueUnit = ValueUnit.UNKNOWN
    actual_raw_text: Optional[str] = None

    provider_forecast: Optional[float] = None
    provider_forecast_unit: ValueUnit = ValueUnit.UNKNOWN
    provider_forecast_raw_text: Optional[str] = None
    forecast_source: Optional[str] = None

    previous: Optional[float] = None
    previous_unit: ValueUnit = ValueUnit.UNKNOWN
    previous_raw_text: Optional[str] = None
    revised_previous: Optional[float] = None

    official_actual: Optional[float] = None
    official_actual_unit: ValueUnit = ValueUnit.UNKNOWN
    official_source: Optional[str] = None
    official_vintage_date: Optional[dt.date] = None
    # "LATEST_REVISED": today's best-known value (a default, no-realtime-
    # params FRED query) -- official_vintage_date is when that CURRENT
    # revision became official, NOT when the period was first published.
    # "AS_OF": value(s) as they stood on a specific ALFRED as-of date
    # (official_vintage_date is that exact as-of date) -- the honest way
    # to compare a historical calendar actual against what was actually
    # known/published at a specific point in time. See normalize/fred.py.
    official_vintage_kind: str = "LATEST_REVISED"

    unit: Optional[str] = None  # provider's own display unit label, if any (diagnostic)
    importance: Optional[str] = None

    # -- provenance ------------------------------------------------------
    source: MacroSource
    source_event_id: Optional[str] = None
    source_timestamp: Optional[dt.datetime] = None  # original, un-normalized
    source_timezone: Optional[str] = None  # e.g. "America/New_York", "UNKNOWN"
    source_url: Optional[str] = None
    raw_artifact_checksum: Optional[str] = None  # links back to the immutable raw file this row came from

    # Acquisition time (when the RAW data was fetched/exported) vs.
    # normalization time (when this row was derived from it) are kept
    # separate on purpose -- collapsing them loses the ability to tell
    # "when did we learn this" from "when did we last reprocess it".
    retrieval_timestamp_utc: dt.datetime  # acquisition time of the underlying raw artifact
    normalized_at_utc: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def timestamp_is_trustworthy(self) -> bool:
        """True only for CONFIRMED timestamps. ASSUMED timestamps have a
        value but rest on an unverified default; TENTATIVE/UNRESOLVED
        have no reliable instant at all. Use this (not just a None
        check) before any minute-level join against market data."""
        quality = self.timestamp_quality
        quality_value = quality.value if isinstance(quality, TimestampQuality) else quality
        return quality_value == TimestampQuality.CONFIRMED.value

    @property
    def is_quarantined(self) -> bool:
        return self.release_timestamp_utc is None


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

    timeframe: str = "1min"
    adjustment: str = "raw"

    source: str = "MASSIVE"
    retrieval_timestamp_utc: dt.datetime
    normalized_at_utc: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


class MacroValidationResult(BaseModel):
    event_family: str
    reference_key: str  # release date or reference period used to group sources
    field: str  # which field was compared, e.g. "actual"
    status: ValidationStatus
    values: dict  # {source_name: value}
    units: dict = Field(default_factory=dict)  # {source_name: unit}
    note: Optional[str] = None
