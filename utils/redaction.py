from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"
_SECRET_ENV_KEYS = (
    "DISCORD_TOKEN",
    "DATABASE_URL",
    "FERNET_KEY",
    "TORN_API_KEY",
    "API_KEY",
    "DB_PASSWORD",
)


def redact(value: str | None) -> str:
    text = str(value or "")
    for key in _SECRET_ENV_KEYS:
        text = re.sub(rf"({key}\s*[=:]\s*)([^\s,;]+)", rf"\1{_REDACTED}", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}\b", _REDACTED, text)
    text = re.sub(r"\bpostgres(?:ql)?://[^\s]+", _REDACTED, text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Za-z0-9_-]{43,44}=\b", _REDACTED, text)
    return text


def redact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        upper_key = str(key).upper()
        if any(secret in upper_key for secret in _SECRET_ENV_KEYS):
            clean[key] = _REDACTED
            continue
        if isinstance(value, str):
            clean[key] = redact(value)
        else:
            clean[key] = value
    return clean


# Backward-compatible aliases.
redact_text = redact
redact_mapping = redact_dict
