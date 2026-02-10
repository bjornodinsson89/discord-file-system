from __future__ import annotations

from urllib.parse import urlparse

from .errors import InvalidInput


def validate_positive_int(value: int, *, field_name: str, min_value: int = 1, max_value: int | None = None) -> int:
    if not isinstance(value, int):
        raise InvalidInput(f"{field_name} must be an integer")
    if value < min_value:
        raise InvalidInput(f"{field_name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise InvalidInput(f"{field_name} must be <= {max_value}")
    return value


def validate_discord_id(value: int) -> int:
    return validate_positive_int(value, field_name="Discord ID")


def validate_torn_id(value: int) -> int:
    return validate_positive_int(value, field_name="Torn ID")


def validate_guild_id(value: int) -> int:
    return validate_positive_int(value, field_name="Guild ID")


def validate_url(value: str, *, required_host_contains: str | None = None) -> str:
    url = sanitize_text(value, field_name="URL", max_length=500)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InvalidInput("URL must be a valid http(s) URL")
    if required_host_contains and required_host_contains not in parsed.netloc.lower():
        raise InvalidInput("URL must point to the expected host")
    return url


def sanitize_text(value: str | None, *, field_name: str, max_length: int) -> str:
    text = (value or "").strip()
    if not text:
        raise InvalidInput(f"{field_name} is required")
    if len(text) > max_length:
        raise InvalidInput(f"{field_name} must be at most {max_length} characters")
    return text
