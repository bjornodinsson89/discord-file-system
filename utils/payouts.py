"""Helpers for parsing and formatting insurer payout item strings."""

from __future__ import annotations

from collections import OrderedDict
import re
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


_TOKEN_SPLIT_RE = re.compile(r"[,;\n|]+")


class PayoutParseError(ValueError):
    """Raised when a payout string cannot be parsed."""


def _normalize_item_name(raw: str) -> str:
    return " ".join((raw or "").strip().lower().replace("_", " ").split())


def _parse_token(token: str) -> tuple[str, str] | None:
    if "=" in token:
        left, right = token.split("=", 1)
        return left.strip(), right.strip()
    if ":" in token:
        left, right = token.split(":", 1)
        return left.strip(), right.strip()

    match = re.fullmatch(r"(.+?)\s+x\s*(\d+)", token, re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2)

    match = re.fullmatch(r"x\s*(\d+)\s+(.+)", token, re.IGNORECASE)
    if match:
        return match.group(2).strip(), match.group(1)

    match = re.fullmatch(r"(\d+)\s*x\s*(.+)", token, re.IGNORECASE)
    if match:
        return match.group(2).strip(), match.group(1)

    match = re.fullmatch(r"(.+?)\s+(\d+)", token)
    if match:
        return match.group(1).strip(), match.group(2)

    match = re.fullmatch(r"(\d+)\s+(.+)", token)
    if match:
        return match.group(2).strip(), match.group(1)

    return None


def parse_payout_string(text: str) -> List[Dict[str, int | str]]:
    """Parse an insurer payout string into JSONB-ready payout item objects."""
    source = (text or "").strip()
    if not source:
        return []

    merged: "OrderedDict[str, int]" = OrderedDict()
    tokens = [token.strip() for token in _TOKEN_SPLIT_RE.split(source) if token.strip()]
    if not tokens:
        return []

    index = 0
    visible_index = 0
    while index < len(tokens):
        token = tokens[index]
        visible_index += 1

        parsed = _parse_token(token)
        if parsed is None and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            combined = f"{token} {tokens[index + 1]}"
            parsed = _parse_token(combined)
            if parsed is not None:
                index += 1

        if parsed is None:
            raise PayoutParseError(
                f"Token #{visible_index} ('{token}') is invalid. Use item=qty or item:qty (example: xanax=4, edvd=6)."
            )

        left, qty_text = parsed
        item_name = _normalize_item_name(left)

        if not item_name:
            raise PayoutParseError(
                f"Token #{visible_index} ('{token}') is missing an item name. Example: xanax=4."
            )

        canonical = _ALIAS_TO_CANONICAL.get(item_name)
        if not canonical:
            valid = "xanax, edvd (erotic dvd), ecstasy"
            raise PayoutParseError(f"Unknown payout item '{left.strip()}'. Valid items: {valid}.")

        try:
            qty = int(qty_text)
        except (TypeError, ValueError):
            raise PayoutParseError(
                f"Token #{visible_index} ('{token}') has invalid quantity '{qty_text}'. Quantity must be an integer >= 1."
            )

        if qty < 1:
            raise PayoutParseError(
                f"Token #{visible_index} ('{token}') has invalid quantity {qty}. Quantity must be >= 1."
            )

        merged[canonical] = merged.get(canonical, 0) + qty
        index += 1

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
