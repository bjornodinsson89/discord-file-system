from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import List

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"
REEL_PATH = ROOT / "assets" / "slots" / "slot_reel.png"

_FACE: Image.Image | None = None
_REEL: Image.Image | None = None

# Your reel cycle order (top->bottom in ONE cycle of the strip)
REEL_CYCLE = [281, 865, 206, 394, 366]  # lion, mistletoe, xanax, cash/brick, edvd
CYCLE_LEN = len(REEL_CYCLE)
ITEM_H = 180
SPEED = 6  # pixels per frame step; ConnorSwis used 6
FRAME_COUNT = ITEM_H // SPEED  # 30 frames like ConnorSwis
FRAME_DURATION_MS = 50  # ConnorSwis duration

# Paste positions copied from ConnorSwis:
# x = 25 + rw*col, y = 100 - (SPEED*i*s)
X_START = 25
Y_BASE = 100


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


def _symbol_index(item_id: int) -> int:
    try:
        return REEL_CYCLE.index(int(item_id))
    except ValueError:
        return 0


def _pick_stop_for_symbol(items: int, desired_symbol_index: int) -> int:
    # Find an s in [1, items-1] such that (1+s)%CYCLE_LEN == desired_symbol_index
    # Avoid s==items (ConnorSwis avoided last)
    for s in range(1, items):
        if (1 + s) % CYCLE_LEN == desired_symbol_index:
            if s != items:
                return s
    return 1


def render_slots_gif(final_reels: List[int]) -> bytes:
    face = _load_face()
    reel = _load_reel()
    rw, rh = reel.size

    if rh % ITEM_H != 0:
        cropped_h = rh - (rh % ITEM_H)
        if cropped_h < ITEM_H:
            raise ValueError(f"slot_reel.png height must be at least {ITEM_H}. Got rh={rh}.")
        reel = reel.crop((0, 0, rw, cropped_h))
        rh = cropped_h
    items = rh // ITEM_H

    # Determine s1/s2/s3 from backend reels so animation lands on correct final symbols
    sym_idx = [
        _symbol_index(final_reels[0]),
        _symbol_index(final_reels[1]),
        _symbol_index(final_reels[2]),
    ]
    s1 = _pick_stop_for_symbol(items, sym_idx[0])
    s2 = _pick_stop_for_symbol(items, sym_idx[1])
    s3 = _pick_stop_for_symbol(items, sym_idx[2])

    images: list[Image.Image] = []

    # Create frames exactly like ConnorSwis
    for i in range(1, FRAME_COUNT + 1):
        bg = Image.new("RGBA", face.size, color=(255, 255, 255, 255))
        bg.paste(reel, (X_START + rw * 0, Y_BASE - (SPEED * i * s1)), reel)
        bg.paste(reel, (X_START + rw * 1, Y_BASE - (SPEED * i * s2)), reel)
        bg.paste(reel, (X_START + rw * 2, Y_BASE - (SPEED * i * s3)), reel)

        # Composite the face on top
        bg.alpha_composite(face)
        images.append(bg)

    out = BytesIO()
    images[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=FRAME_DURATION_MS,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return out.getvalue()
