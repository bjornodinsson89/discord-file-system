import re
from collections import OrderedDict

_NON_ALNUM_SPACE_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_ITEM_SEGMENT_SPLIT_RE = re.compile(r"[,;\n|]+")

_DISPLAY_NAMES = {
    "xanax": "Xanax",
    "erotic_dvd": "Erotic DvD",
}

_DISPLAY_EMOJIS = {
    "xanax": "💊",
    "erotic_dvd": "📀",
}

_DISPLAY_ORDER = {
    "xanax": 0,
    "erotic_dvd": 1,
}


def normalize_token(raw: str) -> str:
    value = str(raw or "").lower().strip().replace("_", " ")
    value = _NON_ALNUM_SPACE_RE.sub("", value)
    value = _MULTI_SPACE_RE.sub(" ", value).strip()
    return value


def parse_payment_type(raw: str, *, allow_free: bool) -> str:
    token = normalize_token(raw)
    token_compact = token.replace(" ", "")

    if token in {"xanax", "xan", "xans"} or token_compact in {"xanax", "xan", "xans"}:
        return "xanax"

    if token in {"erotic dvd", "edvd", "e dvd", "eroticdvd"} or token_compact in {"eroticdvd", "edvd"}:
        return "erotic_dvd"

    if allow_free and token in {"giveaway", "free", "0", "none"}:
        return "free"

    if allow_free:
        raise ValueError("Enter giveaway, xanax, or edvd")
    raise ValueError("Enter xanax or edvd")


def display_payment_options(*, allow_free: bool) -> str:
    if allow_free:
        return "Giveaway | Xanax 💊 | Erotic DvD 📀"
    return "Xanax 💊 | Erotic DvD 📀"


def _parse_segment(segment: str) -> tuple[str, int] | None:
    text = str(segment or "").strip()
    if not text:
        return None

    for separator in ("=", ":"):
        if separator in text:
            left, right = text.split(separator, 1)
            return left.strip(), int(str(right).strip())

    match = re.fullmatch(r"(.+?)\s+x\s*(\d+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2))

    match = re.fullmatch(r"x\s*(\d+)\s+(.+)", text, re.IGNORECASE)
    if match:
        return match.group(2).strip(), int(match.group(1))

    match = re.fullmatch(r"(\d+)\s*x\s*(.+)", text, re.IGNORECASE)
    if match:
        return match.group(2).strip(), int(match.group(1))

    match = re.fullmatch(r"(.+?)\s+(\d+)", text)
    if match:
        return match.group(1).strip(), int(match.group(2))

    match = re.fullmatch(r"(\d+)\s+(.+)", text)
    if match:
        return match.group(2).strip(), int(match.group(1))

    return None


def parse_item_quantities(raw: str) -> dict[str, int]:
    source = str(raw or "").strip()
    if not source:
        return {}

    merged: "OrderedDict[str, int]" = OrderedDict()
    segments = [part.strip() for part in _ITEM_SEGMENT_SPLIT_RE.split(source) if part.strip()]
    index = 0
    while index < len(segments):
        segment = segments[index]
        parsed = _parse_segment(segment)
        if parsed is None and index + 1 < len(segments) and segments[index + 1].isdigit():
            parsed = _parse_segment(f"{segment} {segments[index + 1]}")
            if parsed is not None:
                index += 1
        if parsed is None:
            raise ValueError("Enter payment as item and quantity (example: xanax=3, edvd=2)")

        item_raw, qty = parsed
        try:
            item = parse_payment_type(item_raw, allow_free=False)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        if qty < 1 or qty > 10:
            raise ValueError("Enter quantity from 1 to 10")

        merged[item] = merged.get(item, 0) + qty
        index += 1

    return dict(merged)


def format_item_quantities(qty: dict[str, int]) -> str:
    entries = [
        (item, int(amount))
        for item, amount in (qty or {}).items()
        if item in _DISPLAY_NAMES and int(amount) > 0
    ]
    if not entries:
        return ""

    entries.sort(key=lambda item_qty: (_DISPLAY_ORDER.get(item_qty[0], 99), item_qty[0]))
    return " + ".join(
        f"{_DISPLAY_EMOJIS[item]} {amount} {_DISPLAY_NAMES[item]}"
        for item, amount in entries
    )
