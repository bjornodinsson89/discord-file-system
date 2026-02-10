"""Helpers for parsing and formatting insurer payout item strings."""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, List

_CANONICAL_TO_LABEL = {
    "xanax": "Xanax",
    "erotic_dvd": "eDVD",
    "ecstasy": "Ecstasy",
}

_CANONICAL_TO_SHORT_KEY = {
    "xanax": "xanax",
    "erotic_dvd": "edvd",
    "ecstasy": "ecstasy",
}

_ALIAS_TO_CANONICAL = {
    "xanax": "xanax",
    "xans": "xanax",
    "xan": "xanax",
    "edvd": "erotic_dvd",
    "dvd": "erotic_dvd",
    "eroticd": "erotic_dvd",
    "erotic_dvd": "erotic_dvd",
    "erotic dvd": "erotic_dvd",
    "ecstasy": "ecstasy",
    "e": "ecstasy",
    "xtc": "ecstasy",
}


class PayoutParseError(ValueError):
    """Raised when a payout string cannot be parsed."""


def _normalize_item_name(raw: str) -> str:
    return " ".join((raw or "").strip().lower().replace("_", " ").split())


def parse_payout_string(text: str) -> List[Dict[str, int | str]]:
    """Parse an insurer payout string into JSONB-ready payout item objects."""
    source = (text or "").strip()
    if not source:
        return []

    merged: "OrderedDict[str, int]" = OrderedDict()
    tokens = [token.strip() for token in source.split(",") if token.strip()]
    if not tokens:
        return []

    for index, token in enumerate(tokens, start=1):
        if "=" in token:
            left, right = token.split("=", 1)
        elif ":" in token:
            left, right = token.split(":", 1)
        else:
            raise PayoutParseError(
                f"Token #{index} ('{token}') is invalid. Use item=qty or item:qty (example: xanax=4, edvd=6)."
            )

        item_name = _normalize_item_name(left)
        qty_text = right.strip()

        if not item_name:
            raise PayoutParseError(
                f"Token #{index} ('{token}') is missing an item name. Example: xanax=4."
            )

        canonical = _ALIAS_TO_CANONICAL.get(item_name)
        if not canonical:
            valid = "xanax, edvd (erotic dvd), ecstasy"
            raise PayoutParseError(
                f"Unknown payout item '{left.strip()}'. Valid items: {valid}."
            )

        try:
            qty = int(qty_text)
        except (TypeError, ValueError):
            raise PayoutParseError(
                f"Token #{index} ('{token}') has invalid quantity '{qty_text}'. Quantity must be an integer >= 1."
            )

        if qty < 1:
            raise PayoutParseError(
                f"Token #{index} ('{token}') has invalid quantity {qty}. Quantity must be >= 1."
            )

        merged[canonical] = merged.get(canonical, 0) + qty

    return [{"item": item, "qty": qty} for item, qty in merged.items()]


def payout_items_to_string(items: Iterable[Dict[str, int | str]] | None) -> str:
    """Convert payout item JSON data to a compact editable string."""
    if not items:
        return ""

    parts: List[str] = []
    for entry in items:
        item = str((entry or {}).get("item") or "").strip()
        qty = int((entry or {}).get("qty") or 0)
        if item in _CANONICAL_TO_SHORT_KEY and qty > 0:
            parts.append(f"{_CANONICAL_TO_SHORT_KEY[item]}={qty}")

    return ", ".join(parts)


def payout_items_to_human(items: Iterable[Dict[str, int | str]] | None, max_len: int = 220) -> str:
    """Convert payout item JSON data to one-line human text for Discord embeds."""
    if not items:
        return "Not set"

    parts: List[str] = []
    for entry in items:
        item = str((entry or {}).get("item") or "").strip()
        qty = int((entry or {}).get("qty") or 0)
        if item in _CANONICAL_TO_LABEL and qty > 0:
            parts.append(f"{qty}x {_CANONICAL_TO_LABEL[item]}")

    if not parts:
        return "Not set"

    joined = " • ".join(parts)
    if len(joined) <= max_len:
        return joined
    return f"{joined[: max_len - 1].rstrip()}…"
