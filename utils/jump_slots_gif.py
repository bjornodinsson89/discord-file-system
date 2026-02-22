from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Tuple

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"
REEL_PATH = ROOT / "assets" / "slots" / "slot_reel.png"

_FACE: Image.Image | None = None
_REEL: Image.Image | None = None


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


@dataclass(frozen=True)
class SlotsGeometry:
    reel_x: Tuple[int, int, int]
    reel_y_top: int
    window_y: int
    window_h: int
    reel_w: int
    reel_step: int


DEFAULT_GEOM = SlotsGeometry(
    reel_x=(120, 238, 356),
    reel_y_top=230,
    window_y=230,
    window_h=110,
    reel_w=96,
    reel_step=110,
)

REEL_ORDER = [281, 865, 206, 394, 366]


def symbol_index(item_id: int) -> int:
    try:
        return REEL_ORDER.index(item_id)
    except ValueError:
        return 0


def render_slots_gif(
    final_reels: List[int],
    *,
    frames: int = 24,
    duration_ms: int = 45,
    geom: SlotsGeometry = DEFAULT_GEOM,
) -> bytes:
    face = _load_face()
    reel = _load_reel()
    width, height = face.size

    reels = [int(v) for v in final_reels[:3]]
    while len(reels) < 3:
        reels.append(REEL_ORDER[0])

    idx = [symbol_index(reels[0]), symbol_index(reels[1]), symbol_index(reels[2])]
    spins = 3
    stop_px = [(i + spins * len(REEL_ORDER)) * geom.reel_step for i in idx]

    imgs: List[Image.Image] = []
    for frame_idx in range(frames):
        t = (frame_idx + 1) / frames
        ease = 1 - (1 - t) * (1 - t)

        bg = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for col in range(3):
            y_off = int(stop_px[col] * ease)
            paste_y = geom.reel_y_top - y_off
            bg.paste(reel, (geom.reel_x[col], paste_y), reel)

        window = bg.crop((0, geom.window_y, width, geom.window_y + geom.window_h))
        bg2 = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        bg2.paste(window, (0, geom.window_y))

        out = Image.alpha_composite(bg2, face)
        out_p = out.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
        imgs.append(out_p)

    buf = BytesIO()
    imgs[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=imgs[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return buf.getvalue()
