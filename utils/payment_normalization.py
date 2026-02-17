import re

_NON_ALNUM_SPACE_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def normalize_token(raw: str) -> str:
    value = str(raw or "").lower().strip().replace("_", " ")
    value = _NON_ALNUM_SPACE_RE.sub("", value)
    value = _MULTI_SPACE_RE.sub(" ", value).strip()
    return value


def parse_payment_type(raw: str, *, allow_free: bool) -> str:
    token = normalize_token(raw)

    if token in {"xanax", "xan", "xans"}:
        return "xanax"

    if token in {"erotic dvd", "edvd", "e dvd", "eroticdvd"}:
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
