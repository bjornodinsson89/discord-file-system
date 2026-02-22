from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
CASINO_ITEMS_DIR = ROOT / "assets" / "casino_items"
GENERATED_REEL_PATH = ROOT / "assets" / "slots" / "slot_reel_generated_v2.png"


def build_reel_strip(symbol_ids: list[int], *, cell: int = 180, repeats: int = 6) -> Path:
    if not symbol_ids:
        raise ValueError("symbol_ids cannot be empty")
    if cell <= 0:
        raise ValueError("cell must be > 0")
    if repeats <= 0:
        raise ValueError("repeats must be > 0")

    cells: list[Image.Image] = []
    for item_id in symbol_ids:
        icon_path = CASINO_ITEMS_DIR / f"{int(item_id)}.png"
        if not icon_path.exists():
            raise FileNotFoundError(f"Missing casino item icon: {icon_path}")

        icon = Image.open(icon_path).convert("RGBA")
        ratio = min(cell / icon.width, cell / icon.height)
        new_w = max(1, round(icon.width * ratio))
        new_h = max(1, round(icon.height * ratio))
        fitted = icon.resize((new_w, new_h), Image.Resampling.LANCZOS)

        canvas = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        px = (cell - fitted.width) // 2
        py = (cell - fitted.height) // 2
        canvas.alpha_composite(fitted, (px, py))

        draw = ImageDraw.Draw(canvas)
        line_width = max(2, cell // 72)
        y = cell - 1
        draw.line((0, y, cell, y), fill=(255, 255, 255, 255), width=line_width)
        cells.append(canvas)

    cycle_height = cell * len(cells)
    strip = Image.new("RGBA", (cell, cycle_height * repeats), (0, 0, 0, 0))

    for repeat_idx in range(repeats):
        base_y = repeat_idx * cycle_height
        for symbol_idx, symbol_cell in enumerate(cells):
            strip.alpha_composite(symbol_cell, (0, base_y + symbol_idx * cell))

    GENERATED_REEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    strip.save(GENERATED_REEL_PATH, format="PNG")
    return GENERATED_REEL_PATH


def ensure_reel_strip(symbol_ids: list[int] | None = None) -> Path:
    ids = (
        [394, 707, 281, 197, 366, 865, 206] if symbol_ids is None else [int(v) for v in symbol_ids]
    )
    if GENERATED_REEL_PATH.exists():
        return GENERATED_REEL_PATH
    return build_reel_strip(ids)
