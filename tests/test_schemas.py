import datetime as dt

from src.data.schemas import MacroEvent, MacroSource, TimestampQuality, ValueUnit


def _base_event(**overrides) -> MacroEvent:
    fields = dict(
        event_id="ff:2024-01-11::CPI m/m",
        event_family="CPI_MOM",
        indicator="CPI MoM",
        release_timestamp_utc=dt.datetime(2024, 1, 11, 13, 30, tzinfo=dt.timezone.utc),
        timestamp_quality=TimestampQuality.CONFIRMED,
        actual=0.3,
        actual_unit=ValueUnit.PERCENT,
        provider_forecast=0.4,
        forecast_source="FOREX_FACTORY",
        previous=0.2,
        source=MacroSource.FOREX_FACTORY,
        source_timestamp=dt.datetime(2024, 1, 11, 13, 30),
        source_timezone="America/New_York",
        retrieval_timestamp_utc=dt.datetime.now(dt.timezone.utc),
    )
    fields.update(overrides)
    return MacroEvent(**fields)


def test_provider_forecast_is_not_labeled_economist_consensus():
    event = _base_event()
    assert not hasattr(event, "economist_consensus")
    assert event.forecast_source == "FOREX_FACTORY"


def test_timestamp_trustworthy_true_only_for_confirmed_quality():
    assert _base_event(timestamp_quality=TimestampQuality.CONFIRMED).timestamp_is_trustworthy is True
    assert _base_event(timestamp_quality=TimestampQuality.ASSUMED).timestamp_is_trustworthy is False
    assert _base_event(timestamp_quality=TimestampQuality.TENTATIVE).timestamp_is_trustworthy is False
    assert _base_event(timestamp_quality=TimestampQuality.UNRESOLVED).timestamp_is_trustworthy is False


def test_unresolved_timestamp_is_quarantined_via_null_release_timestamp():
    event = _base_event(release_timestamp_utc=None, timestamp_quality=TimestampQuality.UNRESOLVED)
    assert event.release_timestamp_utc is None
    assert event.is_quarantined is True
    assert event.timestamp_is_trustworthy is False


def test_resolved_timestamp_is_not_quarantined():
    event = _base_event()
    assert event.is_quarantined is False


def test_model_dump_roundtrip_via_dict():
    event = _base_event()
    dumped = event.model_dump()
    rebuilt = MacroEvent(**dumped)
    assert rebuilt.event_id == event.event_id
    assert rebuilt.actual == event.actual
    assert rebuilt.source == event.source
    assert rebuilt.timestamp_quality == event.timestamp_quality


def test_optional_fields_default_to_none():
    event = _base_event()
    assert event.official_actual is None
    assert event.official_source is None
    assert event.revised_previous is None
    assert event.reference_period is None


def test_value_unit_defaults_to_unknown_when_not_specified():
    event = _base_event(actual_unit=ValueUnit.UNKNOWN)
    assert event.actual_unit == ValueUnit.UNKNOWN.value


def test_acquisition_and_normalization_time_are_distinct_fields():
    acquired = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    event = _base_event(retrieval_timestamp_utc=acquired)
    assert event.retrieval_timestamp_utc == acquired
    assert event.normalized_at_utc >= acquired
