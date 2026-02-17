from datetime import datetime

import pytest

from cogs.events import _parse_optional_session_start


def test_parse_optional_session_start_supports_legacy_24h_formats():
    aware, scheduled = _parse_optional_session_start("2099-02-18 21:00", {"timezone": "UTC"})
    assert aware == datetime(2099, 2, 18, 21, 0, tzinfo=aware.tzinfo)
    assert scheduled == "2099-02-18 21:00"

    aware_slash, scheduled_slash = _parse_optional_session_start("2099/02/18 09", {"timezone": "UTC"})
    assert aware_slash == datetime(2099, 2, 18, 9, 0, tzinfo=aware_slash.tzinfo)
    assert scheduled_slash == "2099-02-18 09:00"


def test_parse_optional_session_start_supports_am_pm_variants():
    aware, scheduled = _parse_optional_session_start("2099-02-18 9pm", {"timezone": "UTC"})
    assert aware == datetime(2099, 2, 18, 21, 0, tzinfo=aware.tzinfo)
    assert scheduled == "2099-02-18 21:00"

    aware_min, scheduled_min = _parse_optional_session_start("2099/02/18 12:05 AM", {"timezone": "UTC"})
    assert aware_min == datetime(2099, 2, 18, 0, 5, tzinfo=aware_min.tzinfo)
    assert scheduled_min == "2099-02-18 00:05"

    aware_noon, scheduled_noon = _parse_optional_session_start("2099-02-18 12:30 pm", {"timezone": "UTC"})
    assert aware_noon == datetime(2099, 2, 18, 12, 30, tzinfo=aware_noon.tzinfo)
    assert scheduled_noon == "2099-02-18 12:30"


@pytest.mark.parametrize(
    "value",
    [
        "2099-13-01 10:00",
        "2099-02-32 10:00",
        "2099-02-18 24:00",
        "2099-02-18 12:60",
        "2099-02-18 00pm",
        "2099-02-18 13pm",
        "2099-02-18 nope",
    ],
)
def test_parse_optional_session_start_rejects_invalid(value):
    with pytest.raises(ValueError, match="invalid start"):
        _parse_optional_session_start(value, {"timezone": "UTC"})
