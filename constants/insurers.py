"""Insurer category constants and helpers."""

from __future__ import annotations

INSURER_CATEGORIES: tuple[str, ...] = (
    "99k jump",
    "Happy jump",
    "Xanax stack",
    "Ecstasy only",
    "Multi day",
    "2 hours after purchase",
)

INSURER_CATEGORY_SET = set(INSURER_CATEGORIES)


def normalize_insurer_categories(values: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """Return categories in canonical order, filtered to allowed values."""
    if not values:
        return []
    selected = {str(value).strip() for value in values if str(value).strip() in INSURER_CATEGORY_SET}
    return [category for category in INSURER_CATEGORIES if category in selected]
