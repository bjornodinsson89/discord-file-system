from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import math

from PIL import Image, ImageDraw

from utils.slots_layout import get_scaled_reel_boxes

ROOT = Path(__file__).resolve().parent.parent
FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"
CUSTOM_STRIP_PATH = ROOT / "assets" / "slots" / "reel_strip.png"
_FACE: Image.Image | None = None
_REEL: Image.Image | None = None
_CELL_H_RAW: int | None = None
_LAYOUT_CACHE: (
    tuple[
        Image.Image,
        Image.Image,
        list[tuple[int, int, int, int]],
        list[tuple[int, int]],
        int,
        tuple[int, int, int, int],
        bool,
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
GIF_BYTE_LIMIT = 7_800_000

ENCODE_ATTEMPTS: list[tuple[int, int, int]] = [
    (900, 40, 128),
    (850, 36, 112),
    (800, 32, 96),
    (760, 28, 80),
    (720, 24, 72),
]


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
        if reel.height < CYCLE_LEN:
            raise RuntimeError(
                "Custom reel strip too short for one cycle: "
                f"need at least {CYCLE_LEN}px, got {reel.height}px"
            )

        cell_h_raw = reel.height // CYCLE_LEN
        if cell_h_raw <= 0:
            raise RuntimeError(
                "Custom reel strip has invalid cell height: "
                f"reel.height={reel.height}, cycle_len={CYCLE_LEN}"
            )

        cycle_h = cell_h_raw * CYCLE_LEN
        if reel.height < cycle_h:
            raise RuntimeError(
                "Custom reel strip too short for one cycle: "
                f"expected at least {cycle_h}px, got {reel.height}px"
            )
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
    int,
    tuple[int, int, int, int],
    bool,
]:
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is not None:
        return _LAYOUT_CACHE

    face = _load_face()
    reel = _load_reel()
    reel_boxes = _detect_reel_boxes(face)

    anchor_x0, anchor_y0, _, _ = reel_boxes[0]
    sample_x = min(face.width - 1, anchor_x0 + 10)
    sample_y = min(face.height - 1, anchor_y0 + 10)
    bg_px = face.getpixel((sample_x, sample_y))
    bg_rgba = (int(bg_px[0]), int(bg_px[1]), int(bg_px[2]), 255)
    if len(reel_boxes) != 3:
        raise ValueError("Invalid reel boxes layout")
    if any((x1 - x0) <= 0 or (y1 - y0) <= 0 for x0, y0, x1, y1 in reel_boxes):
        raise ValueError("Invalid reel boxes layout")

    if _CELL_H_RAW is None:
        raise ValueError("Reel cell height metadata missing")
    cell_h_raw = _CELL_H_RAW

    win_sizes = [(x1 - x0, y1 - y0) for x0, y0, x1, y1 in reel_boxes]
    _LAYOUT_CACHE = (
        face,
        reel,
        reel_boxes,
        win_sizes,
        cell_h_raw,
        bg_rgba,
        _has_transparent_windows(face, reel_boxes),
    )
    return _LAYOUT_CACHE


def _has_transparent_windows(
    face: Image.Image, reel_boxes: list[tuple[int, int, int, int]]
) -> bool:
    alpha = face.getchannel("A")
    px = alpha.load()
    for x0, y0, x1, y1 in reel_boxes:
        transparent = 0
        total = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                total += 1
                if px[x, y] < 10:
                    transparent += 1
        if total > 0 and (transparent / total) >= 0.40:
            return True
    return False


def _detect_reel_boxes(face: Image.Image) -> list[tuple[int, int, int, int]]:
    alpha = face.getchannel("A")
    px = alpha.load()
    w, h = face.size

    y0 = int(h * 0.20)
    y1 = int(h * 0.55)
    x0 = int(w * 0.05)
    x1 = int(w * 0.95)
    roi_h = max(1, y1 - y0)

    col_transparent: list[int] = []
    for x in range(x0, x1):
        c = 0
        for y in range(y0, y1):
            if px[x, y] < 10:
                c += 1
        col_transparent.append(c)

    col_mask = [c >= int(roi_h * 0.45) for c in col_transparent]
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for i, ok in enumerate(col_mask):
        if ok and run_start is None:
            run_start = i
        elif not ok and run_start is not None:
            if (i - run_start) >= max(8, w // 40):
                runs.append((run_start, i))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(col_mask)))

    if len(runs) >= 3:
        runs = sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:3]
        runs = sorted(runs, key=lambda r: r[0])
        boxes: list[tuple[int, int, int, int]] = []
        for rx0, rx1 in runs:
            wx0, wx1 = x0 + rx0, x0 + rx1
            run_w = max(1, wx1 - wx0)
            row_transparent: list[int] = []
            for y in range(y0, y1):
                c = 0
                for x in range(wx0, wx1):
                    if px[x, y] < 10:
                        c += 1
                row_transparent.append(c)
            row_mask = [c >= int(run_w * 0.55) for c in row_transparent]
            row_start: int | None = None
            row_runs: list[tuple[int, int]] = []
            for i, ok in enumerate(row_mask):
                if ok and row_start is None:
                    row_start = i
                elif not ok and row_start is not None:
                    if (i - row_start) >= max(8, h // 30):
                        row_runs.append((row_start, i))
                    row_start = None
            if row_start is not None:
                row_runs.append((row_start, len(row_mask)))
            if row_runs:
                ry0, ry1 = max(row_runs, key=lambda r: r[1] - r[0])
                boxes.append((wx0, y0 + ry0, wx1, y0 + ry1))
        if len(boxes) == 3:
            return boxes

    return get_scaled_reel_boxes(face.width, face.height)


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


def _render_centered_cell_from_strip(
    strip_source: Image.Image,
    idx: int,
    cell_h: int,
    win_w: int,
    win_h: int,
) -> Image.Image:
    cell_w = strip_source.width
    y0 = idx * cell_h
    cell = strip_source.crop((0, y0, cell_w, y0 + cell_h))
    target_h = max(1, int(round(win_h * 0.90)))
    scale = target_h / max(1, cell.height)
    scaled_w = max(1, int(round(cell.width * scale)))
    scaled = cell.resize((scaled_w, target_h), Image.Resampling.LANCZOS)

    if scaled.width > win_w:
        crop_l = (scaled.width - win_w) // 2
        scaled = scaled.crop((crop_l, 0, crop_l + win_w, scaled.height))

    seg = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
    left = (win_w - scaled.width) // 2
    top = (win_h - scaled.height) // 2
    seg.paste(scaled, (left, top), scaled)
    return seg


def _render_window_from_offset(
    strip_source: Image.Image,
    offset: float,
    cell_h: int,
    win_w: int,
    win_h: int,
) -> Image.Image:
    step = max(1, int(cell_h))
    whole = math.floor(offset / step)
    top_idx = int(whole) % CYCLE_LEN
    frac = float(offset - (whole * step))
    shift = int(round((frac / step) * win_h))

    top_cell = _render_centered_cell_from_strip(strip_source, top_idx, cell_h, win_w, win_h)
    next_cell = _render_centered_cell_from_strip(
        strip_source, (top_idx + 1) % CYCLE_LEN, cell_h, win_w, win_h
    )
    canvas = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
    canvas.paste(top_cell, (0, -shift), top_cell)
    canvas.paste(next_cell, (0, win_h - shift), next_cell)
    return canvas


def maybe_debug_stopped_segments(
    face: Image.Image,
    reel_scaled: Image.Image,
    reel_boxes: list[tuple[int, int, int, int]],
    win_sizes: list[tuple[int, int]],
    cell_h_scaled: int,
) -> None:
    if os.getenv("SLOTS_DEBUG_STOP") != "1":
        return

    debug = face.copy()
    draw = ImageDraw.Draw(debug)
    for box in reel_boxes:
        draw.rectangle(box, outline=(0, 255, 0, 255), width=3)

    for i, (x0, y0, _, _) in enumerate(reel_boxes):
        win_w, win_h = win_sizes[i]
        symbol_idx = i % CYCLE_LEN
        seg = _render_centered_cell_from_strip(
            strip_source=reel_scaled,
            idx=symbol_idx,
            cell_h=cell_h_scaled,
            win_w=win_w,
            win_h=win_h,
        )
        print(f"SLOTS_DEBUG_STOP reel={i} win_size=({win_w}, {win_h}) seg_size={seg.size}")
        debug.paste(seg, (x0, y0), seg)

    out_path = ROOT / "tmp" / "slots_debug_stopped_segments.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    debug.save(out_path, format="PNG")
    print(f"SLOTS_DEBUG_STOP wrote {out_path}")


def _downscale(im: Image.Image, max_w: int) -> Image.Image:
    if max_w <= 0:
        return im
    if im.width <= max_w:
        return im
    ratio = max_w / im.width
    new_h = max(1, round(im.height * ratio))
    return im.resize((max_w, new_h), Image.Resampling.LANCZOS)


def _quantize_with_shared_palette(frames: list[Image.Image], colors: int) -> list[Image.Image]:
    palette_base = frames[0].convert("P", palette=Image.Palette.ADAPTIVE, colors=colors)
    output: list[Image.Image] = [palette_base]

    for fr in frames[1:]:
        try:
            fr_p = fr.convert("RGBA").convert(
                "P", palette=palette_base.palette, dither=Image.Dither.NONE
            )
        except Exception:
            fr_p = fr.convert("RGBA").quantize(palette=palette_base, dither=Image.Dither.NONE)
        output.append(fr_p)
    return output


def _compose_frame_fast(
    face: Image.Image,
    strip_source: Image.Image,
    reel_boxes: list[tuple[int, int, int, int]],
    win_sizes: list[tuple[int, int]],
    offsets: list[float],
    stopped: list[bool] | None = None,
    stop_idxs: list[int] | None = None,
    cell_h_scaled: int | None = None,
    reels_behind_face: bool = True,
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

        if stopped[i]:
            seg = _render_centered_cell_from_strip(
                strip_source=strip_source,
                idx=stop_idxs[i],
                cell_h=cell_h_scaled,
                win_w=win_w,
                win_h=win_h,
            )
        else:
            seg = _render_window_from_offset(strip_source, off, cell_h_scaled, win_w, win_h)

        reels_canvas.paste(seg, (x0, y0), seg)

    if reels_behind_face:
        return Image.alpha_composite(reels_canvas, face)
    return Image.alpha_composite(face, reels_canvas)


def maybe_save_seed_debug_image() -> None:
    if not os.getenv("SEED_DEBUG"):
        return

    face, reel_scaled, reel_boxes, win_sizes, cell_h_scaled, _, _ = _layout()
    debug = face.copy()
    draw = ImageDraw.Draw(debug)

    for box in reel_boxes:
        draw.rectangle(box, outline=(255, 0, 0, 255), width=4)

    for i, (x0, y0, _, _) in enumerate(reel_boxes):
        win_w, win_h = win_sizes[i]
        symbol_idx = i % CYCLE_LEN
        seg = _render_centered_cell_from_strip(
            strip_source=reel_scaled,
            idx=symbol_idx,
            cell_h=cell_h_scaled,
            win_w=win_w,
            win_h=win_h,
        )
        debug.paste(seg, (x0, y0), seg)

    out_path = ROOT / "tmp" / "seed_debug_slots_layout.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    debug.save(out_path, format="PNG")


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
    face, reel_scaled, reel_boxes, win_sizes, cell_h_scaled, _, reels_behind_face = _layout()
    del jackpot_pool, balance, bet
    normalized = _normalize_reels(reels)
    stop_idxs = [symbol_index(item) for item in normalized]
    frame = _compose_frame_fast(
        face=face,
        strip_source=reel_scaled,
        reel_boxes=reel_boxes,
        win_sizes=win_sizes,
        offsets=[0.0, 0.0, 0.0],
        stopped=[True, True, True],
        stop_idxs=stop_idxs,
        cell_h_scaled=cell_h_scaled,
        reels_behind_face=reels_behind_face,
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

    maybe_save_seed_debug_image()
    face, reel_scaled, reel_boxes, win_sizes, cell_h_scaled, bg_rgba, reels_behind_face = _layout()
    maybe_debug_stopped_segments(face, reel_scaled, reel_boxes, win_sizes, cell_h_scaled)
    normalized = _normalize_reels(final_reels)
    stop_idxs = [symbol_index(item) for item in normalized]
    del jackpot_pool, balance, bet
    starts_stops = [
        _start_and_stop_for_item(item, cell_h_scaled, win_sizes[idx][1])
        for idx, item in enumerate(normalized)
    ]
    starts = [start for start, _ in starts_stops]
    stops_px = [stop for _, stop in starts_stops]
    total_ms = max(2, int(frames)) * max(40, min(150, int(duration_ms)))
    fallback_max_w = max(1, int(max_w))
    fallback_frames = max(2, int(frames))
    fallback_colors = max(32, min(256, int(palette_colors)))
    attempts = ENCODE_ATTEMPTS or [(fallback_max_w, fallback_frames, fallback_colors)]

    best_bytes = 2**31 - 1
    best_attempt: tuple[int, int, int] | None = None

    for attempt_max_w, attempt_frames, attempt_colors in attempts:
        total_frames = max(2, int(attempt_frames))
        colors = max(32, min(256, int(attempt_colors)))
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
                reel_boxes=reel_boxes,
                win_sizes=win_sizes,
                offsets=offsets,
                stopped=stopped_mask,
                stop_idxs=stop_idxs,
                cell_h_scaled=cell_h_scaled,
                reels_behind_face=reels_behind_face,
            )
            rgba_frame = _downscale(rgba_frame, max_w=attempt_max_w)
            rgba_frames.append(rgba_frame)

        rgb_frames = [_to_rgb(fr) for fr in rgba_frames]
        images = _quantize_with_shared_palette(rgb_frames, colors=colors)

        per_frame_duration = max(1, round(total_ms / total_frames))
        out = BytesIO()
        images[0].save(
            out,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=per_frame_duration,
            disposal=2,
            loop=0,
            optimize=True,
        )
        gif_bytes = out.getvalue()
        gif_len = len(gif_bytes)
        if gif_len < best_bytes:
            best_bytes = gif_len
            best_attempt = (attempt_max_w, total_frames, colors)
        if gif_len <= GIF_BYTE_LIMIT:
            print(
                f"render_slots_gif attempt ok max_w={attempt_max_w} frames={total_frames} "
                f"colors={colors} bytes={gif_len}"
            )
            return gif_bytes

    raise ValueError(
        "gif_too_large "
        f"smallest_bytes={best_bytes} limit={GIF_BYTE_LIMIT} "
        f"last_attempt=max_w:{best_attempt[0]},frames:{best_attempt[1]},colors:{best_attempt[2]}"
    )
