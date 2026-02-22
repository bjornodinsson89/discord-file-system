from __future__ import annotations

from io import BytesIO
from pathlib import Path
from statistics import median

from PIL import Image

from utils.jump_slots_assets import ensure_reel_strip

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"

_FACE: Image.Image | None = None
_REEL: Image.Image | None = None

REEL_CYCLE = [394, 707, 281, 197, 366, 865, 206]
REEL_PATH = ensure_reel_strip(REEL_CYCLE)
CYCLE_LEN = len(REEL_CYCLE)
DEFAULT_SPINS = 4
DEFAULT_FRAMES = 84
DEFAULT_DURATION_MS = 55


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


def detect_cell_height(reel: Image.Image) -> int:
    gray = reel.convert("L")
    w, h = gray.size
    px = gray.load()

    row_mean: list[float] = []
    for y in range(h):
        total = 0
        for x in range(w):
            total += px[x, y]
        row_mean.append(total / w)

    peaks = [idx for idx, v in enumerate(row_mean) if v > 240]
    if len(peaks) < 2:
        return max(1, h // CYCLE_LEN)

    groups: list[int] = [peaks[0]]
    for y in peaks[1:]:
        if y - groups[-1] > 1:
            groups.append(y)

    distances = [b - a for a, b in zip(groups, groups[1:], strict=False) if (b - a) > 0]
    if not distances:
        return max(1, h // CYCLE_LEN)

    detected = int(median(distances))
    # Divider detection can pick tiny decorative highlights; clamp to a sane strip cell size.
    min_reasonable = max(1, h // (CYCLE_LEN * 2))
    if detected < min_reasonable:
        return max(1, h // CYCLE_LEN)
    return detected


def _layout() -> tuple[Image.Image, Image.Image, tuple[int, int, int, int], int, list[int]]:
    face = _load_face()
    reel = _load_reel()

    x0, y0, x1, y1 = detect_window_box(face)
    window_w = x1 - x0

    padding_x = int(window_w * 0.06)
    gap = int(window_w * 0.03)
    col_w = (window_w - 2 * padding_x - 2 * gap) // 3
    if col_w <= 0:
        raise ValueError("Invalid slots window layout dimensions")

    scale = col_w / reel.width
    reel_scaled_h = max(1, round(reel.height * scale))
    reel_scaled = reel.resize((col_w, reel_scaled_h), Image.Resampling.LANCZOS)

    cell_h = detect_cell_height(reel)
    cell_h_scaled = max(1, round(cell_h * scale))

    repeats = 8
    tiled = Image.new("RGBA", (col_w, reel_scaled.height * repeats), (0, 0, 0, 0))
    for idx in range(repeats):
        tiled.paste(reel_scaled, (0, idx * reel_scaled.height), reel_scaled)

    col_x0 = x0 + padding_x
    col_x = [col_x0 + i * (col_w + gap) for i in range(3)]
    return face, tiled, (x0, y0, x1, y1), cell_h_scaled, col_x


def _normalize_reels(reels3: list[int]) -> list[int]:
    out = [int(v) for v in reels3][:3]
    while len(out) < 3:
        out.append(REEL_CYCLE[0])
    return out


def symbol_index(item_id: int) -> int:
    if item_id not in REEL_CYCLE:
        raise ValueError(f"Unsupported reel symbol id: {item_id}")
    return REEL_CYCLE.index(item_id)


def _target_px(item_id: int, cell_h_scaled: int, spins: int = DEFAULT_SPINS) -> int:
    idx = symbol_index(item_id)
    return (spins * CYCLE_LEN + idx) * cell_h_scaled


def _compose_frame(
    face: Image.Image,
    tiled: Image.Image,
    window_box: tuple[int, int, int, int],
    col_x: list[int],
    offsets: list[int],
) -> Image.Image:
    x0, y0, x1, y1 = window_box
    reels_layer = Image.new("RGBA", face.size, (0, 0, 0, 0))
    for i, off in enumerate(offsets):
        reels_layer.paste(tiled, (col_x[i], y0 - int(off)), tiled)

    reels_canvas = Image.new("RGBA", face.size, (0, 0, 0, 0))
    reels_canvas.paste(reels_layer.crop((x0, y0, x1, y1)), (x0, y0))
    out = Image.alpha_composite(reels_canvas, face)
    return out


def animation_seconds(
    frames: int = DEFAULT_FRAMES, duration_ms: int = DEFAULT_DURATION_MS
) -> float:
    return max(0.25, (max(2, int(frames)) * int(duration_ms)) / 1000.0)


def render_idle_png(reels: list[int]) -> bytes:
    face, tiled, window_box, cell_h_scaled, col_x = _layout()
    normalized = _normalize_reels(reels)
    offsets = [_target_px(item, cell_h_scaled) for item in normalized]
    frame = _compose_frame(face, tiled, window_box, col_x, offsets)
    out = BytesIO()
    frame.save(out, format="PNG")
    return out.getvalue()


def render_slots_gif(
    final_reels: list[int], frames: int = DEFAULT_FRAMES, duration_ms: int = DEFAULT_DURATION_MS
) -> bytes:
    face, tiled, window_box, cell_h_scaled, col_x = _layout()
    normalized = _normalize_reels(final_reels)
    targets = [_target_px(item, cell_h_scaled) for item in normalized]
    total_frames = max(2, int(frames))
    stop_left = max(1, int(total_frames * 0.45))
    stop_mid = max(stop_left + 1, int(total_frames * 0.70))
    stop_right = max(stop_mid + 1, int(total_frames * 0.90))
    stops = [stop_left, stop_mid, stop_right]

    images: list[Image.Image] = []
    for f in range(1, total_frames + 1):
        offsets: list[int] = []
        for reel_idx in range(3):
            stop_f = stops[reel_idx]
            target_px = targets[reel_idx]
            if f >= stop_f:
                off = target_px
            else:
                p = f / stop_f
                ease = 1 - (1 - p) ** 3
                off = int(target_px * ease)
            offsets.append(off)

        rgba_frame = _compose_frame(face, tiled, window_box, col_x, offsets)
        images.append(rgba_frame.convert("P", palette=Image.Palette.ADAPTIVE))

    out = BytesIO()
    images[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=max(40, min(80, int(duration_ms))),
        disposal=2,
        optimize=False,
    )
    return out.getvalue()
