"""Single-source constants for 99k signup/payment states."""

SIGNUP_STATUS_RESERVED = "reserved"
SIGNUP_STATUS_PAID = "paid"
SIGNUP_STATUS_CANCELLED = "cancelled"
SIGNUP_STATUS_EXPIRED = "expired"
SIGNUP_STATUS_COMPLETED = "completed"
SIGNUP_STATUS_NOT_COMPLETED = "not_completed"

_LEGACY_STATUS_ALIASES = {
    "signed_up": SIGNUP_STATUS_PAID,
    "confirmed": SIGNUP_STATUS_PAID,
}

SIGNUP_ACTIVE_STATUSES = {
    SIGNUP_STATUS_PAID,
    SIGNUP_STATUS_COMPLETED,
    SIGNUP_STATUS_NOT_COMPLETED,
}

SIGNUP_TRANSIENT_STATUSES = {
    SIGNUP_STATUS_RESERVED,
    SIGNUP_STATUS_CANCELLED,
    SIGNUP_STATUS_EXPIRED,
}

SIGNUP_ALLOWED_STATUSES = SIGNUP_ACTIVE_STATUSES | SIGNUP_TRANSIENT_STATUSES
ALLOWED_SIGNUP_STATUSES = SIGNUP_ALLOWED_STATUSES

PAYMENT_STATUS_PENDING = "pending"
PAYMENT_STATUS_VERIFIED = "verified"
PAYMENT_ALLOWED_STATUSES = {PAYMENT_STATUS_PENDING, PAYMENT_STATUS_VERIFIED}


def normalize_signup_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return _LEGACY_STATUS_ALIASES.get(normalized, normalized)


def validate_status(value: str) -> str:
    normalized = normalize_signup_status(value)
    if normalized not in ALLOWED_SIGNUP_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_SIGNUP_STATUSES)}.")
    return normalized
