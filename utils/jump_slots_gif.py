from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Iterable

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"
REEL_PATH = ROOT / "assets" / "slots" / "slot_reel.png"

_FACE: Image.Image | None = None
_REEL: Image.Image | None = None

REEL_CYCLE = [281, 865, 206, 394, 366]
CYCLE_LEN = len(REEL_CYCLE)
WINDOW_BOX = (70, 180, 430, 290)  # x0, y0, x1, y1
COL_GAP = 8
PADDING_X = 12
BASE_CELL_HEIGHT = 180
DEFAULT_SPINS = 3


def _load_face() -> Image.Image:
    global _FACE
    if _FACE is None:
        _FACE = Image.open(FACE_PATH).convert("RGBA")
    return _FACE


def _load_reel() -> Image.Image:
    global _REEL
    if _REEL is None:
        _REEL = Image.open(REEL_PATH).convert("RGBA")
    return _REEL


def symbol_index(item_id: int) -> int:
    try:
        return REEL_CYCLE.index(int(item_id))
    except ValueError:
        return 0


def _stop_px(idx: int, cell_h: int, spins: int = DEFAULT_SPINS) -> int:
    return (int(idx) + spins * CYCLE_LEN) * cell_h


def _layout() -> tuple[Image.Image, Image.Image, int, int, int, int, list[int]]:
    face = _load_face()
    reel = _load_reel()

    x0, y0, x1, y1 = WINDOW_BOX
    window_w = x1 - x0
    col_w = max(1, (window_w - 2 * PADDING_X - 2 * COL_GAP) // 3)
    scale = col_w / reel.width
    scaled_h = max(1, int(reel.height * scale))
    reel_scaled = reel.resize((col_w, scaled_h), Image.Resampling.LANCZOS)
    cell_h = max(1, int(BASE_CELL_HEIGHT * scale))

    repeats = DEFAULT_SPINS + 3
    reel_tiled = Image.new("RGBA", (reel_scaled.width, reel_scaled.height * repeats), (0, 0, 0, 0))
    for i in range(repeats):
        reel_tiled.paste(reel_scaled, (0, i * reel_scaled.height), reel_scaled)

    items = max(1, reel_scaled.height // cell_h)
    col_x = [x0 + PADDING_X + i * (col_w + COL_GAP) for i in range(3)]
    return face, reel_tiled, y0, cell_h, items, y1 - y0, col_x


def _clip_to_window(layer: Image.Image) -> Image.Image:
    clipped = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    clipped.paste(layer.crop(WINDOW_BOX), WINDOW_BOX)
    return clipped


def _normalize_reels(reels3: Iterable[int]) -> list[int]:
    out = [int(v) for v in reels3][:3]
    while len(out) < 3:
        out.append(REEL_CYCLE[0])
    return out


def render_idle_png(reels3: list[int]) -> bytes:
    face, reel_scaled, window_y, cell_h, _items, _window_h, col_x = _layout()
    reels = _normalize_reels(reels3)

    reels_layer = Image.new("RGBA", face.size, (0, 0, 0, 0))
    for i, symbol in enumerate(reels):
        idx = symbol_index(symbol)
        reels_layer.paste(reel_scaled, (col_x[i], window_y - _stop_px(idx, cell_h)), reel_scaled)

    clipped = _clip_to_window(reels_layer)
    frame = face.copy()
    frame.alpha_composite(clipped)

    out = BytesIO()
    frame.save(out, format="PNG")
    return out.getvalue()


def render_slots_gif(final_reels: list[int], frames: int = 24, duration_ms: int = 45) -> bytes:
    face, reel_scaled, window_y, cell_h, _items, _window_h, col_x = _layout()
    reels = _normalize_reels(final_reels)
    targets = [_stop_px(symbol_index(symbol), cell_h) for symbol in reels]

    images: list[Image.Image] = []
    total_frames = max(2, int(frames))

    for frame_idx in range(total_frames):
        t = frame_idx / (total_frames - 1)
        ease = 1 - (1 - t) ** 3

        reels_layer = Image.new("RGBA", face.size, (0, 0, 0, 0))
        for col in range(3):
            y_off = int(targets[col] * ease)
            reels_layer.paste(reel_scaled, (col_x[col], window_y - y_off), reel_scaled)

        clipped = _clip_to_window(reels_layer)
        frame = face.copy()
        frame.alpha_composite(clipped)
        images.append(frame)

    out = BytesIO()
    images[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=max(1, int(duration_ms)),
        loop=1,
        disposal=2,
        optimize=False,
    )
    return out.getvalue()
