from __future__ import annotations

from io import BytesIO
from pathlib import Path
import math

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"
CUSTOM_STRIP_PATH = ROOT / "assets" / "slots" / "reel_strip.png"

_FACE: Image.Image | None = None
_REEL: Image.Image | None = None
_LAYOUT_CACHE: (
    tuple[
        Image.Image,
        Image.Image,
        Image.Image,
        tuple[int, int, int, int],
        int,
        int,
        int,
        list[int],
        tuple[int, int, int, int],
    ]
    | None
) = None

REEL_CYCLE = [281, 865, 206, 197, 366]
CYCLE_LEN = len(REEL_CYCLE)
BASE_SPINS = 2
EXTRA_SPINS = 7
SPIN_FRAMES = 40
SPIN_DURATION_MS = 110
DEFAULT_FRAMES = SPIN_FRAMES
DEFAULT_DURATION_MS = SPIN_DURATION_MS


def _load_face() -> Image.Image:
    global _FACE
    if _FACE is None:
        _FACE = Image.open(FACE_PATH).convert("RGBA")
    return _FACE


def _load_reel() -> Image.Image:
    global _REEL
    if _REEL is None:
        if not CUSTOM_STRIP_PATH.exists():
            raise RuntimeError(f"Missing custom reel strip: {CUSTOM_STRIP_PATH}")
        _REEL = Image.open(CUSTOM_STRIP_PATH).convert("RGBA")
    return _REEL


def detect_window_box(face: Image.Image) -> tuple[int, int, int, int]:
    gray = face.convert("L")
    w, h = gray.size
    px = gray.load()

    y_start = int(h * 0.15)
    y_end = int(h * 0.75)
    dark_rows: list[int] = []
    for y in range(y_start, y_end):
        dark = 0
        for x in range(w):
            if px[x, y] <= 40:
                dark += 1
        if dark > int(w * 0.55):
            dark_rows.append(y)

    if not dark_rows:
        raise ValueError("Unable to detect slots window rows in slot_face.png")

    y0, y1 = min(dark_rows), max(dark_rows)
    window_h = max(1, y1 - y0)

    x_start = int(w * 0.05)
    x_end = int(w * 0.95)
    dark_cols: list[int] = []
    for x in range(x_start, x_end):
        dark = 0
        for y in range(y0, y1):
            if px[x, y] <= 40:
                dark += 1
        if dark > int(window_h * 0.55):
            dark_cols.append(x)

    if not dark_cols:
        raise ValueError("Unable to detect slots window columns in slot_face.png")

    x0, x1 = min(dark_cols), max(dark_cols)
    inset = 6
    x0 = max(0, x0 + inset)
    y0 = max(0, y0 + inset)
    x1 = min(w, x1 - inset)
    y1 = min(h, y1 - inset)
    return x0, y0, x1, y1


def reset_slots_render_cache() -> None:
    global _REEL, _LAYOUT_CACHE
    _REEL = None
    _LAYOUT_CACHE = None


def _layout() -> tuple[
    Image.Image,
    Image.Image,
    Image.Image,
    tuple[int, int, int, int],
    int,
    int,
    int,
    list[int],
    tuple[int, int, int, int],
]:
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is not None:
        return _LAYOUT_CACHE

    face = _load_face()
    reel = _load_reel()

    x0, y0, x1, y1 = detect_window_box(face)
    sample_x = min(face.width - 1, x0 + 10)
    sample_y = min(face.height - 1, y0 + 10)
    bg_px = face.getpixel((sample_x, sample_y))
    bg_rgba = (int(bg_px[0]), int(bg_px[1]), int(bg_px[2]), 255)
    window_w = x1 - x0
    window_h = y1 - y0

    # Slightly reduce horizontal padding/gap so reel symbols appear a bit larger.
    padding_x = int(window_w * 0.05)
    gap = int(window_w * 0.025)
    col_w = int((window_w - padding_x * 2 - gap * 2) / 3)
    if col_w <= 0:
        raise ValueError("Invalid slots window layout dimensions")

    scale = col_w / reel.width
    reel_scaled = reel.resize(
        (
            col_w,
            max(1, round(reel.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )
    cell_h_scaled = reel_scaled.height // CYCLE_LEN
    if cell_h_scaled <= 0:
        raise ValueError("Invalid scaled cell height")

    # Placeholder tiled image; actual render paths rebuild with required spin height.
    tiled = reel_scaled

    col_x0 = x0 + padding_x
    col_x = [col_x0 + i * (col_w + gap) for i in range(3)]

    _LAYOUT_CACHE = (
        face,
        reel_scaled,
        tiled,
        (x0, y0, x1, y1),
        window_h,
        col_w,
        cell_h_scaled,
        col_x,
        bg_rgba,
    )
    return _LAYOUT_CACHE


def _normalize_reels(reels3: list[int]) -> list[int]:
    out = [int(v) for v in reels3][:3]
    while len(out) < 3:
        out.append(REEL_CYCLE[0])
    return out


def symbol_index(item_id: int) -> int:
    if item_id not in REEL_CYCLE:
        raise ValueError(f"Unsupported reel symbol id: {item_id}")
    return REEL_CYCLE.index(item_id)


def _stop_offset_for_symbol(idx: int, cell_h: int, window_h: int) -> float:
    return idx * cell_h + (cell_h / 2.0) - (window_h / 2.0)


def _start_and_stop_for_item(item_id: int, cell_h: int, window_h: int) -> tuple[float, float]:
    idx = symbol_index(item_id)
    stop = (BASE_SPINS * CYCLE_LEN * cell_h) + _stop_offset_for_symbol(idx, cell_h, window_h)
    start = stop + (EXTRA_SPINS * CYCLE_LEN * cell_h)
    return start, stop


def _build_tiled_strip(
    reel_scaled: Image.Image, col_w: int, window_h: int, max_start: float
) -> Image.Image:
    required_px = int(math.ceil(max_start + window_h + 2))
    repeats = max(4, math.ceil(required_px / reel_scaled.height) + 1)
    tiled = Image.new("RGBA", (col_w, reel_scaled.height * repeats), (0, 0, 0, 0))
    for idx in range(repeats):
        tiled.paste(reel_scaled, (0, idx * reel_scaled.height), reel_scaled)
    return tiled


def _render_centered_cell(
    reel_scaled: Image.Image,
    idx: int,
    cell_h: int,
    window_h: int,
    col_w: int,
) -> Image.Image:
    y0 = idx * cell_h
    cell = reel_scaled.crop((0, y0, col_w, y0 + cell_h))
    tile = Image.new("RGBA", (col_w, window_h), (0, 0, 0, 0))
    top = (window_h - cell_h) // 2
    if top >= 0:
        tile.paste(cell, (0, top), cell)
        return tile
    crop_top = (cell_h - window_h) // 2
    cell = cell.crop((0, crop_top, col_w, crop_top + window_h))
    tile.paste(cell, (0, 0), cell)
    return tile


def _downscale(im: Image.Image, max_w: int) -> Image.Image:
    if max_w <= 0:
        return im
    if im.width <= max_w:
        return im
    ratio = max_w / im.width
    new_h = max(1, round(im.height * ratio))
    return im.resize((max_w, new_h), Image.Resampling.LANCZOS)


def _compose_frame_fast(
    face: Image.Image,
    reel_scaled: Image.Image,
    tiled: Image.Image,
    window_box: tuple[int, int, int, int],
    window_h: int,
    col_w: int,
    col_x: list[int],
    offsets: list[float],
    stopped: list[bool] | None = None,
    stop_idxs: list[int] | None = None,
    cell_h_scaled: int | None = None,
) -> Image.Image:
    _, y0, _, _ = window_box
    stopped = stopped or [False, False, False]
    if any(stopped):
        if stop_idxs is None:
            raise ValueError("stop_idxs must be provided when stopped reels are used")
        if cell_h_scaled is None:
            raise ValueError("cell_h_scaled must be provided when stopped reels are used")

    reels_canvas = Image.new("RGBA", face.size, (0, 0, 0, 0))
    for i, off in enumerate(offsets):
        if stopped[i]:
            seg = _render_centered_cell(
                reel_scaled=reel_scaled,
                idx=stop_idxs[i],
                cell_h=cell_h_scaled,
                window_h=window_h,
                col_w=col_w,
            )
        else:
            y = int(round(off))
            y = max(0, min(y, tiled.height - window_h))
            seg = tiled.crop((0, y, col_w, y + window_h))
        reels_canvas.paste(seg, (col_x[i], y0), seg)
    out = Image.alpha_composite(reels_canvas, face)
    return out


def animation_seconds(
    frames: int = DEFAULT_FRAMES, duration_ms: int = DEFAULT_DURATION_MS
) -> float:
    return max(0.25, (max(2, int(frames)) * int(duration_ms)) / 1000.0)


def render_idle_png(reels: list[int], max_w: int = 900) -> bytes:
    face, reel_scaled, _, window_box, window_h, col_w, cell_h_scaled, col_x, _ = _layout()
    normalized = _normalize_reels(reels)
    stop_idxs = [symbol_index(item) for item in normalized]
    offsets = [
        (BASE_SPINS * CYCLE_LEN * cell_h_scaled)
        + _stop_offset_for_symbol(stop_idx, cell_h_scaled, window_h)
        for stop_idx in stop_idxs
    ]
    max_start = max(offsets)
    tiled = _build_tiled_strip(reel_scaled, col_w, window_h, max_start)
    frame = _compose_frame_fast(
        face=face,
        reel_scaled=reel_scaled,
        tiled=tiled,
        window_box=window_box,
        window_h=window_h,
        col_w=col_w,
        col_x=col_x,
        offsets=[0.0, 0.0, 0.0],
        stopped=[True, True, True],
        stop_idxs=stop_idxs,
        cell_h_scaled=cell_h_scaled,
    )
    frame = _downscale(frame, max_w=max_w)
    out = BytesIO()
    frame.save(out, format="PNG")
    return out.getvalue()


def render_slots_gif(
    final_reels: list[int],
    frames: int = 40,
    duration_ms: int = 110,
    max_w: int = 900,
    palette_colors: int = 128,
) -> bytes:
    def _to_rgb(im: Image.Image) -> Image.Image:
        if im.mode == "RGB":
            return im
        if im.mode == "RGBA":
            bg = Image.new("RGBA", im.size, bg_rgba)
            return Image.alpha_composite(bg, im).convert("RGB")
        return im.convert("RGB")

    face, reel_scaled, _, window_box, window_h, col_w, cell_h_scaled, col_x, bg_rgba = _layout()
    normalized = _normalize_reels(final_reels)
    stop_idxs = [symbol_index(item) for item in normalized]
    starts_stops = [_start_and_stop_for_item(item, cell_h_scaled, window_h) for item in normalized]
    starts = [start for start, _ in starts_stops]
    stops_px = [stop for _, stop in starts_stops]
    tiled = _build_tiled_strip(reel_scaled, col_w, window_h, max(starts))
    total_frames = max(2, int(frames))
    stop_left = max(1, int(total_frames * 0.45))
    stop_mid = max(stop_left + 1, int(total_frames * 0.70))
    stop_right = max(stop_mid + 1, int(total_frames * 0.90))
    stops = [stop_left, stop_mid, stop_right]

    rgba_frames: list[Image.Image] = []
    for f in range(1, total_frames + 1):
        offsets: list[float] = []
        for reel_idx in range(3):
            stop_f = stops[reel_idx]
            if f >= stop_f:
                off = stops_px[reel_idx]
            else:
                p = f / stop_f
                ease = 1 - (1 - p) ** 3
                off = starts[reel_idx] - (starts[reel_idx] - stops_px[reel_idx]) * ease
            offsets.append(off)

        stopped_mask = [
            f >= stop_left,
            f >= stop_mid,
            f >= stop_right,
        ]

        rgba_frame = _compose_frame_fast(
            face=face,
            reel_scaled=reel_scaled,
            tiled=tiled,
            window_box=window_box,
            window_h=window_h,
            col_w=col_w,
            col_x=col_x,
            offsets=offsets,
            stopped=stopped_mask,
            stop_idxs=stop_idxs,
            cell_h_scaled=cell_h_scaled,
        )
        rgba_frames.append(_downscale(rgba_frame, max_w=max_w))

    first_rgb = _to_rgb(rgba_frames[0])
    palette_colors_int = max(32, min(256, int(palette_colors)))
    palette_base = first_rgb.convert("P", palette=Image.Palette.ADAPTIVE, colors=palette_colors_int)
    images: list[Image.Image] = [palette_base]
    for fr in rgba_frames[1:]:
        fr_rgb = _to_rgb(fr)
        images.append(fr_rgb.quantize(palette=palette_base, dither=Image.Dither.NONE))

    out = BytesIO()
    images[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=max(40, min(150, int(duration_ms))),
        disposal=2,
        optimize=False,
    )
    return out.getvalue()
