import datetime as dt

from src.data.schemas import MacroEvent, MacroSource


def _base_event(**overrides) -> MacroEvent:
    fields = dict(
        event_id="ff:2024-01-11::CPI m/m",
        event_family="CPI_MOM",
        indicator="CPI MoM",
        release_timestamp_utc=dt.datetime(2024, 1, 11, 13, 30, tzinfo=dt.timezone.utc),
        actual=0.3,
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


def test_timestamp_trustworthy_true_for_known_timezone():
    event = _base_event(source_timezone="America/New_York")
    assert event.timestamp_is_trustworthy is True


def test_timestamp_trustworthy_false_for_unknown_or_server():
    assert _base_event(source_timezone="UNKNOWN").timestamp_is_trustworthy is False
    assert _base_event(source_timezone="SERVER").timestamp_is_trustworthy is False
    assert _base_event(source_timezone=None).timestamp_is_trustworthy is False


def test_model_dump_roundtrip_via_dict():
    event = _base_event()
    dumped = event.model_dump()
    rebuilt = MacroEvent(**dumped)
    assert rebuilt.event_id == event.event_id
    assert rebuilt.actual == event.actual
    assert rebuilt.source == event.source


def test_optional_fields_default_to_none():
    event = _base_event()
    assert event.official_actual is None
    assert event.official_source is None
    assert event.revised_previous is None
