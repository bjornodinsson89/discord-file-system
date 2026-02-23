from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from utils.slots_layout import BASE_FACE_H, get_scaled_layout

ROOT = Path(__file__).resolve().parent.parent
_TEXT_FILL = (242, 246, 255, 255)
_TEXT_SHADOW = (15, 19, 28, 255)


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.ImageFont:
    candidates = (
        ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf",
        ROOT / "assets" / "DejaVuSans-Bold.ttf",
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _scaled_font_size(face_h: int, base_size: int, min_size: int, max_size: int) -> int:
    scale = float(face_h) / float(BASE_FACE_H)
    return _clamp(int(round(base_size * scale)), min_size, max_size)


def _clamp_box(
    box: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    x0 = _clamp(x0, 0, width)
    y0 = _clamp(y0, 0, height)
    x1 = _clamp(x1, 0, width)
    y1 = _clamp(y1, 0, height)
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


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


def _draw_labeled_value_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
) -> None:
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    label_y = y0 + int(h * 0.35)
    value_y = y0 + int(h * 0.73)
    center_x = x0 + (w // 2)

    draw.text(
        (center_x, label_y),
        label,
        font=label_font,
        fill=(225, 233, 247, 255),
        anchor="mm",
        stroke_width=1,
        stroke_fill=_TEXT_SHADOW,
    )
    draw.text(
        (center_x, value_y),
        value,
        font=value_font,
        fill=_TEXT_FILL,
        anchor="mm",
        stroke_width=2,
        stroke_fill=_TEXT_SHADOW,
    )


def draw_paytable(
    face: Image.Image,
    payouts: list[list[int]],
    balance_text: str | None = None,
    bet_text: str | None = None,
) -> Image.Image:
    out = face.copy()
    face_w, face_h = out.size
    layout = get_scaled_layout(face_w, face_h)

    pay_headers = layout["pay_headers"]
    pay_cells = layout["pay_cells"]
    balance_box = layout["balance_box"]
    bet_box = layout["bet_box"]

    draw = ImageDraw.Draw(out)
    header_font = _font(_scaled_font_size(face_h, base_size=34, min_size=18, max_size=52))
    cell_font = _font(_scaled_font_size(face_h, base_size=28, min_size=16, max_size=48))
    label_font = _font(_scaled_font_size(face_h, base_size=16, min_size=10, max_size=28))
    value_font = _font(_scaled_font_size(face_h, base_size=22, min_size=14, max_size=36))

    for header, box in pay_headers:
        _draw_centered_text(draw, _clamp_box(box, face_w, face_h), header, header_font)

    for row_idx, row in enumerate(payouts[:6]):
        if row_idx >= len(pay_cells):
            break
        for col_idx, value in enumerate(row[:3]):
            if col_idx >= len(pay_cells[row_idx]):
                break
            _draw_centered_text(
                draw,
                _clamp_box(pay_cells[row_idx][col_idx], face_w, face_h),
                f"{int(value):,}",
                cell_font,
            )

    _draw_labeled_value_box(
        draw,
        _clamp_box(balance_box, face_w, face_h),
        "BALANCE",
        balance_text or "...",
        label_font,
        value_font,
    )
    _draw_labeled_value_box(
        draw,
        _clamp_box(bet_box, face_w, face_h),
        "BET",
        bet_text or "x0",
        label_font,
        value_font,
    )

    return out
