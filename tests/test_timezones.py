from utils.timezones import REGION_MAP, REGION_LABELS, get_region_timezones


def test_timezone_regions_present():
    assert set(REGION_MAP.keys()) == {"americas", "europe", "africa", "asia", "oceania", "utc_offsets"}
    assert set(REGION_LABELS.keys()) == set(REGION_MAP.keys())


def test_timezone_region_lists_non_empty_and_unique():
    for region, zones in REGION_MAP.items():
        assert zones, f"{region} should not be empty"
        assert len(zones) == len(set(zones)), f"{region} contains duplicates"


def test_americas_has_more_than_one_page_for_dropdown_paging():
    zones = get_region_timezones("americas")
    assert len(zones) > 25


def test_utc_offsets_contains_utc_and_fixed_offsets():
    zones = get_region_timezones("utc_offsets")
    assert "UTC" in zones
    assert "Etc/GMT+5" in zones
    assert "Etc/GMT-5" in zones
