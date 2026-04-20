from __future__ import annotations

import base64
import json
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def normalize_for_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Decimal):
        return format(value, "f")

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Enum):
        enum_value = value.value
        if isinstance(enum_value, (str, int, float, bool)) or enum_value is None:
            return enum_value
        return str(enum_value)

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(value).decode("ascii")

    if isinstance(value, dict):
        return {str(k): normalize_for_json(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [normalize_for_json(v) for v in value]

    return str(value)


def json_dumps_safe(value: Any, *, sort_keys: bool = True) -> str:
    normalized = normalize_for_json(value)
    return json.dumps(normalized, sort_keys=sort_keys, separators=(",", ":"), ensure_ascii=False)
