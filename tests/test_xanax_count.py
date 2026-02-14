import pytest

from bot_actions.schemas import CreateSessionRequest


def normalize_xanax_count(value):
    if value is None:
        raise ValueError
    normalized = int(value)
    if normalized < 1 or normalized > 4:
        raise ValueError
    return normalized


def test_create_session_request_accepts_xanax_count_1_to_4():
    req = CreateSessionRequest(
        guild_id=1,
        channel_id=2,
        payment_type="xanax",
        payment_amount=1,
        spots=8,
        xanax_count=4,
    )
    assert req.xanax_count == 4


@pytest.mark.parametrize("value", [0, 5, "abc", "4.0", None])
def test_normalize_xanax_count_rejects_invalid_values(value):
    with pytest.raises((ValueError, TypeError)):
        normalize_xanax_count(value)


@pytest.mark.parametrize("value", [1, 2, 3, 4, "1", "4"])
def test_normalize_xanax_count_accepts_ints_and_numeric_strings(value):
    normalized = normalize_xanax_count(value)
    assert normalized in {1, 2, 3, 4}
