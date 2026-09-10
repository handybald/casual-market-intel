from src.data.event_mapping import load_event_mapping


def test_resolve_mql5_and_forex_factory_names_map_to_same_family():
    mapping = load_event_mapping()

    mql5_entry = mapping.resolve_mql5("Nonfarm Payrolls")
    ff_entry = mapping.resolve_forex_factory("Non-Farm Employment Change")

    assert mql5_entry is not None
    assert ff_entry is not None
    assert mql5_entry.event_family == ff_entry.event_family == "NFP"


def test_resolve_is_case_and_whitespace_insensitive():
    mapping = load_event_mapping()
    assert mapping.resolve_mql5("  nonfarm   payrolls ") is not None
    assert mapping.resolve_mql5("NONFARM PAYROLLS") is not None


def test_unmapped_name_returns_none():
    mapping = load_event_mapping()
    assert mapping.resolve_mql5("Totally Unknown Event") is None
    assert mapping.resolve_forex_factory("Totally Unknown Event") is None


def test_release_bundle_grouping():
    mapping = load_event_mapping()
    cpi_families = set(mapping.families_in_bundle("CPI"))
    assert cpi_families == {"CPI_MOM", "CPI_YOY", "CORE_CPI_MOM", "CORE_CPI_YOY"}

    employment_families = set(mapping.families_in_bundle("EMPLOYMENT"))
    assert "NFP" in employment_families
    assert "UNEMPLOYMENT_RATE" in employment_families


def test_by_family_returns_indicator_name():
    mapping = load_event_mapping()
    entry = mapping.by_family("NFP")
    assert entry.indicator == "Nonfarm Payrolls"
