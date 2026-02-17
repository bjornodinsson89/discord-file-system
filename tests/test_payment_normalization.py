import pytest

from utils.payment_normalization import (
    display_payment_options,
    format_item_quantities,
    normalize_token,
    parse_item_quantities,
    parse_payment_type,
)


def test_normalize_token_variants():
    assert normalize_token("  Erotic_DvD!! ") == "erotic dvd"
    assert normalize_token("xanax___") == "xanax"


def test_parse_payment_type_raffle_allows_free():
    assert parse_payment_type("NONE", allow_free=True) == "free"
    assert parse_payment_type("xans", allow_free=True) == "xanax"
    assert parse_payment_type("eDVD", allow_free=True) == "erotic_dvd"


def test_parse_payment_type_xanax_alias_variants():
    variants = ["XANAX", "xanax", "Xan", "xans", "x a n", "x_a_n", "x-a-n", "Xanax!!!"]
    for variant in variants:
        assert parse_payment_type(variant, allow_free=False) == "xanax"


def test_parse_payment_type_edvd_alias_variants():
    variants = [
        "Erotic DvD",
        "erotic dvd",
        "Erotic DVD",
        "edvd",
        "eDVD",
        "e dvd",
        "e-dvd",
        "e dv d",
        "eroticdvd",
        "erotic_dvd",
        "erotic dv d",
    ]
    for variant in variants:
        assert parse_payment_type(variant, allow_free=False) == "erotic_dvd"


def test_parse_payment_type_paid_rejects_free():
    with pytest.raises(ValueError, match="Enter xanax or edvd"):
        parse_payment_type("free", allow_free=False)


def test_parse_payment_type_unknown_error_messages():
    with pytest.raises(ValueError, match="Enter giveaway, xanax, or edvd"):
        parse_payment_type("cash", allow_free=True)


def test_parse_item_quantities_supported_formats():
    assert parse_item_quantities("xan=4") == {"xanax": 4}
    assert parse_item_quantities("Erotic DvD:3") == {"erotic_dvd": 3}
    assert parse_item_quantities("edvd x3") == {"erotic_dvd": 3}
    assert parse_item_quantities("x3 xan") == {"xanax": 3}
    assert parse_item_quantities("2x edvd") == {"erotic_dvd": 2}
    assert parse_item_quantities("xanax,3; edvd=2") == {"xanax": 3, "erotic_dvd": 2}


def test_parse_item_quantities_sums_duplicates():
    assert parse_item_quantities("xan=2, Xanax 1") == {"xanax": 3}


def test_parse_item_quantities_invalid_quantity_rejected():
    with pytest.raises(ValueError, match="Enter quantity from 1 to 10"):
        parse_item_quantities("xanax=0")
    with pytest.raises(ValueError, match="Enter quantity from 1 to 10"):
        parse_item_quantities("xanax=-1")
    with pytest.raises(ValueError, match="Enter quantity from 1 to 10"):
        parse_item_quantities("xanax=999")


def test_parse_item_quantities_unknown_item_rejected():
    with pytest.raises(ValueError, match="Enter xanax or edvd"):
        parse_item_quantities("weed=2")


def test_format_item_quantities_canonical_output():
    assert format_item_quantities({"xanax": 3}) == "💊 3 Xanax"
    assert format_item_quantities({"erotic_dvd": 2, "xanax": 1}) == "💊 1 Xanax + 📀 2 Erotic DvD"


def test_display_payment_options():
    assert display_payment_options(allow_free=True) == "Giveaway | Xanax 💊 | Erotic DvD 📀"
    assert display_payment_options(allow_free=False) == "Xanax 💊 | Erotic DvD 📀"
