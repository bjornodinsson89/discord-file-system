from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

PAY_HDR = [
    (330, 669, 453, 725),
    (453, 669, 569, 725),
    (569, 669, 695, 725),
]

PAY_CELLS = [
    [(330, 725, 453, 783), (453, 725, 569, 783), (569, 725, 695, 783)],
    [(330, 783, 453, 843), (453, 783, 569, 843), (569, 783, 695, 843)],
    [(330, 843, 453, 903), (453, 843, 569, 903), (569, 843, 695, 903)],
    [(330, 903, 453, 964), (453, 903, 569, 964), (569, 903, 695, 964)],
    [(330, 964, 453, 1023), (453, 964, 569, 1023), (569, 964, 695, 1023)],
    [(330, 1023, 453, 1080), (453, 1023, 569, 1080), (569, 1023, 695, 1080)],
]

BALANCE_BOX = (80, 669, 330, 725)
BET_BOX = (695, 669, 950, 725)

_TEXT_FILL = (242, 246, 255, 255)
_TEXT_SHADOW = (15, 19, 28, 255)


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont:
    candidates = (
        ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf",
        ROOT / "assets" / "DejaVuSans-Bold.ttf",
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
) -> None:
    x0, y0, x1, y1 = box
    draw.text(
        ((x0 + x1) / 2, (y0 + y1) / 2),
        text,
        font=font,
        fill=_TEXT_FILL,
        anchor="mm",
        stroke_width=2,
        stroke_fill=_TEXT_SHADOW,
    )


def _clamp_box(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    x0 = max(0, min(width, x0))
    y0 = max(0, min(height, y0))
    x1 = max(0, min(width, x1))
    y1 = max(0, min(height, y1))
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


def get_paytable_rows(
    bet: int,
    triple_multipliers: dict[int, float],
    symbol_order: list[int],
) -> list[list[str]]:
    rows: list[list[str]] = []
    for symbol_id in symbol_order:
        if symbol_id == 9090:
            rows.append(["JP", "JP", "JP"])
            continue
        multiplier = float(triple_multipliers.get(int(symbol_id), 0.0))
        rows.append([f"{int((bet * col * multiplier)):,}" for col in (1, 2, 3)])
    return rows


def _normalize_triple_multipliers(triple_multipliers: dict[int, float]) -> tuple[tuple[int, float], ...]:
    return tuple(sorted((int(symbol_id), float(mult)) for symbol_id, mult in triple_multipliers.items()))


def _draw_static_paytable(
    face: Image.Image,
    *,
    bet: int,
    triple_key: tuple[tuple[int, float], ...],
    symbol_order: tuple[int, ...],
) -> Image.Image:
    out = face.copy()
    draw = ImageDraw.Draw(out)
    hdr_font = _font(34)
    cell_font = _font(30)
    small_font = _font(28)

    for idx, box in enumerate(PAY_HDR, start=1):
        _draw_centered_text(draw, _clamp_box(box, out.width, out.height), str(idx), hdr_font)

    rows = get_paytable_rows(
        bet=bet,
        triple_multipliers={sid: mult for sid, mult in triple_key},
        symbol_order=list(symbol_order),
    )
    for row_idx, row in enumerate(rows):
        if row_idx >= len(PAY_CELLS):
            break
        for col_idx, value in enumerate(row):
            if col_idx >= len(PAY_CELLS[row_idx]):
                break
            _draw_centered_text(
                draw,
                _clamp_box(PAY_CELLS[row_idx][col_idx], out.width, out.height),
                value,
                cell_font,
            )

    bet_box = _clamp_box(BET_BOX, out.width, out.height)
    _draw_centered_text(draw, bet_box, f"BET: {int(bet):,}", small_font)
    return out


@lru_cache(maxsize=16)
def _cached_static_paytable(
    base_face_bytes: bytes,
    size: tuple[int, int],
    mode: str,
    bet: int,
    triple_key: tuple[tuple[int, float], ...],
    symbol_order: tuple[int, ...],
) -> Image.Image:
    face = Image.frombytes(mode, size, base_face_bytes)
    return _draw_static_paytable(
        face,
        bet=bet,
        triple_key=triple_key,
        symbol_order=symbol_order,
    )


def draw_paytable_on_face(
    face: Image.Image,
    *,
    bet: int,
    balance: int | None,
    triple_multipliers: dict[int, float],
    symbol_order: list[int],
) -> Image.Image:
    triple_key = _normalize_triple_multipliers(triple_multipliers)
    symbol_key = tuple(int(v) for v in symbol_order)
    static = _cached_static_paytable(
        face.tobytes(),
        face.size,
        face.mode,
        int(bet),
        triple_key,
        symbol_key,
    ).copy()

    draw = ImageDraw.Draw(static)
    small_font = _font(28)
    balance_text = "BAL: ..." if balance is None else f"BAL: {int(balance):,}"
    bal_box = _clamp_box(BALANCE_BOX, static.width, static.height)
    _draw_centered_text(draw, bal_box, balance_text, small_font)
    return static
