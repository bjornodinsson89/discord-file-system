import pytest

from utils.payment_normalization import display_payment_options, normalize_token, parse_payment_type


def test_normalize_token_variants():
    assert normalize_token("  Erotic_DvD!! ") == "erotic dvd"
    assert normalize_token("xanax___") == "xanax"


def test_parse_payment_type_raffle_allows_free():
    assert parse_payment_type("NONE", allow_free=True) == "free"
    assert parse_payment_type("xans", allow_free=True) == "xanax"
    assert parse_payment_type("eDVD", allow_free=True) == "erotic_dvd"


def test_parse_payment_type_paid_rejects_free():
    with pytest.raises(ValueError, match="Enter xanax or edvd"):
        parse_payment_type("free", allow_free=False)


def test_parse_payment_type_unknown_error_messages():
    with pytest.raises(ValueError, match="Enter free, xanax, or edvd"):
        parse_payment_type("cash", allow_free=True)


def test_display_payment_options():
    assert display_payment_options(allow_free=True) == "Free | Xanax 💊 | Erotic DvD 📀"
    assert display_payment_options(allow_free=False) == "Xanax 💊 | Erotic DvD 📀"
