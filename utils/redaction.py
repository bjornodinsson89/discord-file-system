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
)


def redact_text(value: str | None) -> str:
    text = str(value or "")
    for key in _SECRET_ENV_KEYS:
        text = re.sub(rf"({key}\s*[=:]\s*)([^\s,;]+)", rf"\1{_REDACTED}", text, flags=re.IGNORECASE)
    return text


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        upper_key = str(key).upper()
        if any(secret in upper_key for secret in _SECRET_ENV_KEYS):
            clean[key] = _REDACTED
            continue
        if isinstance(value, str):
            clean[key] = redact_text(value)
        else:
            clean[key] = value
    return clean
