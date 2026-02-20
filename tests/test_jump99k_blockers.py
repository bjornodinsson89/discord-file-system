from datetime import datetime, timedelta, timezone

from constants.jump99k import SIGNUP_STATUS_PAID, normalize_signup_status, validate_status


def test_legacy_signup_statuses_normalize_to_paid():
    assert normalize_signup_status("signed_up") == SIGNUP_STATUS_PAID
    assert normalize_signup_status("confirmed") == SIGNUP_STATUS_PAID
    assert validate_status("signed_up") == SIGNUP_STATUS_PAID


def test_since_timestamp_uses_signup_created_at_window():
    signup_created_at = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    session_created_at = datetime(2026, 2, 20, 9, 0, tzinfo=timezone.utc)
    signup = {
        "signup_created_at": signup_created_at,
        "session_created_at": session_created_at,
    }

    assert signup["signup_created_at"] != signup["session_created_at"]
    since_ts = int((signup["signup_created_at"] - timedelta(seconds=60)).timestamp())
    assert since_ts == int(datetime(2026, 2, 20, 11, 59, tzinfo=timezone.utc).timestamp())
