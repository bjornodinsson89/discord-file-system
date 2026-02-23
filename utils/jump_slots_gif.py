from __future__ import annotations

from io import BytesIO
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"
CUSTOM_STRIP_PATH = ROOT / "assets" / "slots" / "reel_strip.png"
FACE_SIZE = (1024, 1536)
REEL_BOXES = [
    (101, 398, 349, 612),  # Reel 1 (248x214)
    (392, 398, 630, 612),  # Reel 2 (238x214)
    (673, 398, 924, 612),  # Reel 3 (251x214)
]

PAY_HDR = [
    (330, 669, 453, 725),  # "1"
    (453, 669, 569, 725),  # "2"
    (569, 669, 695, 725),  # "3"
]

PAY_CELLS = [
    [(330, 725, 453, 783), (453, 725, 569, 783), (569, 725, 695, 783)],
    [(330, 783, 453, 843), (453, 783, 569, 843), (569, 783, 695, 843)],
    [(330, 843, 453, 903), (453, 843, 569, 903), (569, 843, 695, 903)],
    [(330, 903, 453, 964), (453, 903, 569, 964), (569, 903, 695, 964)],
    [(330, 964, 453, 1023), (453, 964, 569, 1023), (569, 964, 695, 1023)],
    [(330, 1023, 453, 1080), (453, 1023, 569, 1080), (569, 1023, 695, 1080)],
]

BAL_BOX = (101, 669, 330, 725)
BET_BOX = (695, 669, 924, 725)

TRIPLE_MULT = {
    206: 3.0,
    281: 4.0,
    197: 6.0,
    366: 8.0,
    865: 10.0,
}

_FACE: Image.Image | None = None
_REEL: Image.Image | None = None
_CELL_H_RAW: int | None = None
_LAYOUT_CACHE: (
    tuple[
        Image.Image,
        Image.Image,
        list[tuple[int, int, int, int]],
        list[tuple[int, int]],
        list[tuple[int, int]],
        int,
        tuple[int, int, int, int],
    ]
    | None
) = None

REEL_CYCLE = [9090, 281, 865, 206, 197, 366]
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
    global _REEL, _CELL_H_RAW
    if _REEL is None:
        if not CUSTOM_STRIP_PATH.exists():
            raise RuntimeError(f"Missing custom reel strip: {CUSTOM_STRIP_PATH}")

        reel = Image.open(CUSTOM_STRIP_PATH).convert("RGBA")
        cell_h_raw = reel.width
        cycle_h = cell_h_raw * CYCLE_LEN

        if reel.height < cycle_h:
            raise RuntimeError(
                f"Custom reel strip too short for one cycle: expected at least {cycle_h}px, got {reel.height}px"
            )

        if reel.height != cycle_h:
            reel = reel.crop((0, 0, reel.width, cycle_h))

        _CELL_H_RAW = cell_h_raw
        _REEL = reel
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
    global _REEL, _CELL_H_RAW, _LAYOUT_CACHE
    _REEL = None
    _CELL_H_RAW = None
    _LAYOUT_CACHE = None


def _layout() -> tuple[
    Image.Image,
    Image.Image,
    list[tuple[int, int, int, int]],
    list[tuple[int, int]],
    list[tuple[int, int]],
    int,
    tuple[int, int, int, int],
]:
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is not None:
        return _LAYOUT_CACHE

    face = _load_face()
    reel = _load_reel()

    reel_boxes = REEL_BOXES

    anchor_x0, anchor_y0, _, _ = reel_boxes[0]
    sample_x = min(face.width - 1, anchor_x0 + 10)
    sample_y = min(face.height - 1, anchor_y0 + 10)
    bg_px = face.getpixel((sample_x, sample_y))
    bg_rgba = (int(bg_px[0]), int(bg_px[1]), int(bg_px[2]), 255)
    if len(reel_boxes) != 3:
        raise ValueError("Invalid reel boxes layout")
    if any((x1 - x0) <= 0 or (y1 - y0) <= 0 for x0, y0, x1, y1 in reel_boxes):
        raise ValueError("Invalid reel boxes layout")

    cell_h_raw = reel.width
    reel = reel.crop((0, 0, reel.width, cell_h_raw * CYCLE_LEN))
    max_win_h = max(y1 - y0 for _, y0, _, y1 in reel_boxes)
    max_win_w = max(x1 - x0 for x0, _, x1, _ in reel_boxes)
    scale = min(max_win_w / reel.width, max_win_h / cell_h_raw)
    if scale <= 0:
        raise ValueError("Invalid reel scaling")

    scaled_w = max(1, round(reel.width * scale))
    scaled_h = max(1, round(reel.height * scale))
    reel_scaled = reel.resize(
        (scaled_w, scaled_h),
        Image.Resampling.LANCZOS,
    )
    cell_h_scaled = max(1, int(round(cell_h_raw * scale)))
    if cell_h_scaled <= 0:
        raise ValueError("Invalid scaled cell height")

    win_sizes = [(x1 - x0, y1 - y0) for x0, y0, x1, y1 in reel_boxes]
    reel_offsets = [((w - reel_scaled.width) // 2, (h - cell_h_scaled) // 2) for w, h in win_sizes]

    _LAYOUT_CACHE = (
        face,
        reel_scaled,
        reel_boxes,
        win_sizes,
        reel_offsets,
        cell_h_scaled,
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


def _build_tiled_strip(reel_scaled: Image.Image, repeats: int) -> Image.Image:
    tiled = Image.new("RGBA", (reel_scaled.width, reel_scaled.height * repeats), (0, 0, 0, 0))
    for idx in range(repeats):
        tiled.paste(reel_scaled, (0, idx * reel_scaled.height), reel_scaled)
    return tiled


def _render_centered_cell_from_strip(
    strip_source: Image.Image,
    idx: int,
    cell_h: int,
    win_w: int,
    win_h: int,
    x_off: int,
    y_off: int,
) -> Image.Image:
    cell_w = strip_source.width
    y0 = idx * cell_h
    cell = strip_source.crop((0, y0, cell_w, y0 + cell_h))
    tile = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
    tile.paste(cell, (x_off, y_off), cell)
    return tile


def _build_paytable_values() -> list[list[str]]:
    rows: list[list[str]] = []
    for symbol in REEL_CYCLE:
        if symbol == 9090:
            rows.append(["JP", "JP", "JP"])
            continue
        mult = float(TRIPLE_MULT.get(symbol, 0.0))
        rows.append([str(int(math.floor(bet * mult))) for bet in (1, 2, 3)])
    return rows


def _font(size: int) -> ImageFont.ImageFont:
    for p in (
        ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf",
        ROOT / "assets" / "DejaVuSans-Bold.ttf",
    ):
        if p.exists():
            return ImageFont.truetype(str(p), size=size)
    return ImageFont.load_default()


def _draw_centered_text(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.ImageFont
) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    draw.text(
        (cx, cy),
        text,
        font=font,
        fill=(242, 246, 255, 255),
        anchor="mm",
        stroke_width=2,
        stroke_fill=(15, 19, 28, 255),
    )


def _draw_ui_overlay(
    frame: Image.Image, balance: int | None = None, bet: int | None = None
) -> Image.Image:
    draw = ImageDraw.Draw(frame)
    hdr_font = _font(34)
    cell_font = _font(30)
    tiny_font = _font(22)

    for idx, box in enumerate(PAY_HDR, start=1):
        _draw_centered_text(draw, box, str(idx), hdr_font)

    values = _build_paytable_values()
    for row_idx, row in enumerate(values):
        for col_idx, value in enumerate(row):
            _draw_centered_text(draw, PAY_CELLS[row_idx][col_idx], value, cell_font)

    if balance is not None:
        bx0, by0, bx1, by1 = BAL_BOX
        _draw_centered_text(draw, (bx0, by0, bx1, by0 + ((by1 - by0) // 2)), "BAL", tiny_font)
        _draw_centered_text(
            draw, (bx0, by0 + ((by1 - by0) // 2), bx1, by1), str(int(balance)), tiny_font
        )

    if bet is not None:
        bx0, by0, bx1, by1 = BET_BOX
        _draw_centered_text(draw, (bx0, by0, bx1, by0 + ((by1 - by0) // 2)), "BET", tiny_font)
        _draw_centered_text(
            draw, (bx0, by0 + ((by1 - by0) // 2), bx1, by1), str(int(bet)), tiny_font
        )

    return frame


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
    strip_source: Image.Image,
    tiled: Image.Image,
    reel_boxes: list[tuple[int, int, int, int]],
    win_sizes: list[tuple[int, int]],
    reel_offsets: list[tuple[int, int]],
    offsets: list[float],
    stopped: list[bool] | None = None,
    stop_idxs: list[int] | None = None,
    cell_h_scaled: int | None = None,
    balance: int | None = None,
    bet: int | None = None,
) -> Image.Image:
    stopped = stopped or [False, False, False]
    if any(stopped):
        if stop_idxs is None:
            raise ValueError("stop_idxs must be provided when stopped reels are used")
        if cell_h_scaled is None:
            raise ValueError("cell_h_scaled must be provided when stopped reels are used")

    reels_canvas = Image.new("RGBA", face.size, (0, 0, 0, 0))
    for i, off in enumerate(offsets):
        x0, y0, x1, y1 = reel_boxes[i]
        win_w, win_h = win_sizes[i]
        reel_x, reel_y = reel_offsets[i]

        if stopped[i]:
            seg = _render_centered_cell_from_strip(
                strip_source=strip_source,
                idx=stop_idxs[i],
                cell_h=cell_h_scaled,
                win_w=win_w,
                win_h=win_h,
                x_off=reel_x,
                y_off=reel_y,
            )
        else:
            y = int(round(off))
            y = max(0, min(y, tiled.height - win_h))
            strip_seg = tiled.crop((0, y, strip_source.width, y + win_h))
            seg = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
            seg.paste(strip_seg, (reel_x, 0), strip_seg)

        reels_canvas.paste(seg, (x0, y0), seg)

    out = Image.alpha_composite(reels_canvas, face)
    out = _draw_ui_overlay(out, balance=balance, bet=bet)
    return out


def animation_seconds(
    frames: int = DEFAULT_FRAMES, duration_ms: int = DEFAULT_DURATION_MS
) -> float:
    return max(0.25, (max(2, int(frames)) * int(duration_ms)) / 1000.0)


def render_idle_png(
    reels: list[int],
    max_w: int = 900,
    balance: int | None = None,
    bet: int | None = None,
    jackpot_pool: int | None = None,
) -> bytes:
    face, reel_scaled, reel_boxes, win_sizes, reel_offsets, cell_h_scaled, _ = _layout()
    del jackpot_pool
    tiled = _build_tiled_strip(reel_scaled, repeats=4)
    normalized = _normalize_reels(reels)
    stop_idxs = [symbol_index(item) for item in normalized]
    frame = _compose_frame_fast(
        face=face,
        strip_source=reel_scaled,
        tiled=tiled,
        reel_boxes=reel_boxes,
        win_sizes=win_sizes,
        reel_offsets=reel_offsets,
        offsets=[0.0, 0.0, 0.0],
        stopped=[True, True, True],
        stop_idxs=stop_idxs,
        cell_h_scaled=cell_h_scaled,
        balance=balance,
        bet=bet,
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
    balance: int | None = None,
    bet: int | None = None,
    jackpot_pool: int | None = None,
) -> bytes:
    def _to_rgb(im: Image.Image) -> Image.Image:
        if im.mode == "RGB":
            return im
        if im.mode == "RGBA":
            bg = Image.new("RGBA", im.size, bg_rgba)
            return Image.alpha_composite(bg, im).convert("RGB")
        return im.convert("RGB")

    face, reel_scaled, reel_boxes, win_sizes, reel_offsets, cell_h_scaled, bg_rgba = _layout()
    normalized = _normalize_reels(final_reels)
    stop_idxs = [symbol_index(item) for item in normalized]
    del jackpot_pool
    starts_stops = [
        _start_and_stop_for_item(item, cell_h_scaled, win_sizes[idx][1])
        for idx, item in enumerate(normalized)
    ]
    starts = [start for start, _ in starts_stops]
    stops_px = [stop for _, stop in starts_stops]
    max_window_h = max(h for _, h in win_sizes)
    needed = int(math.ceil(max(starts) + max_window_h + 2))
    repeats = max(4, math.ceil(needed / reel_scaled.height) + 1)
    tiled = _build_tiled_strip(reel_scaled, repeats=repeats)

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
            strip_source=reel_scaled,
            tiled=tiled,
            reel_boxes=reel_boxes,
            win_sizes=win_sizes,
            reel_offsets=reel_offsets,
            offsets=offsets,
            stopped=stopped_mask,
            stop_idxs=stop_idxs,
            cell_h_scaled=cell_h_scaled,
            balance=balance,
            bet=bet,
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
