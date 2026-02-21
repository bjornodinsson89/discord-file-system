from datetime import datetime, timezone

import pytest

from cogs.events import _parse_optional_session_start


def test_parse_optional_session_start_supports_discord_timestamp_input():
    aware, scheduled, used_fallback = _parse_optional_session_start("<t:1760000000:F>", {})
    assert aware == datetime.fromtimestamp(1760000000, tz=timezone.utc)
    assert scheduled == "<t:1760000000:F>"
    assert used_fallback is False


def test_parse_optional_session_start_supports_offset_datetimes():
    aware, scheduled, used_fallback = _parse_optional_session_start("2026-02-20 8:00pm -0700", {})
    assert aware == datetime(2026, 2, 21, 3, 0, tzinfo=timezone.utc)
    assert scheduled == f"<t:{int(aware.timestamp())}:F>"
    assert used_fallback is False

    aware_iso, _, used_fallback_iso = _parse_optional_session_start("2026-02-20T20:00:00-07:00", {})
    assert aware_iso == datetime(2026, 2, 21, 3, 0, tzinfo=timezone.utc)
    assert used_fallback_iso is False


def test_parse_optional_session_start_interprets_naive_date_time_as_utc():
    aware, scheduled, used_fallback = _parse_optional_session_start("2099-02-18 9pm", {})
    assert aware == datetime(2099, 2, 18, 21, 0, tzinfo=timezone.utc)
    assert scheduled == f"<t:{int(aware.timestamp())}:F>"
    assert used_fallback is True


@pytest.mark.parametrize(
    ("value", "min_seconds", "max_seconds"),
    [
        ("in 90m", 89 * 60, 91 * 60),
        ("in 2h", 119 * 60, 121 * 60),
        ("in 1d 3h", 26 * 3600, 28 * 3600),
    ],
)
def test_parse_optional_session_start_supports_relative_inputs(value, min_seconds, max_seconds):
    before = datetime.now(timezone.utc).timestamp()
    aware, scheduled, used_fallback = _parse_optional_session_start(value, {})
    delta_seconds = aware.timestamp() - before

    assert min_seconds <= delta_seconds <= max_seconds
    assert scheduled == f"<t:{int(aware.timestamp())}:F>"
    assert used_fallback is False


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
        _parse_optional_session_start(value, {})


def test_parse_optional_session_start_interprets_naive_input_using_host_timezone():
    aware, scheduled, used_fallback = _parse_optional_session_start(
        "2026-02-20 5:30pm",
        {},
        host_timezone_name="America/Denver",
    )
    assert aware == datetime(2026, 2, 21, 0, 30, tzinfo=timezone.utc)
    assert scheduled == f"<t:{int(aware.timestamp())}:F>"
    assert used_fallback is False


def test_parse_optional_session_start_falls_back_when_host_timezone_invalid():
    aware, scheduled, used_fallback = _parse_optional_session_start(
        "2026-02-20 5:30pm",
        {},
        host_timezone_name="Not/A_Timezone",
    )
    assert aware == datetime(2026, 2, 20, 17, 30, tzinfo=timezone.utc)
    assert scheduled == f"<t:{int(aware.timestamp())}:F>"
    assert used_fallback is True
