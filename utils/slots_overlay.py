from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FACE_PATH = ROOT / "assets" / "slots" / "slot_face.png"


@dataclass(frozen=True)
class _OverlayLayout:
    reel_boxes: list[tuple[int, int, int, int]]
    header_boxes: list[tuple[int, int, int, int]]
    cell_boxes: list[list[tuple[int, int, int, int]]]
    balance_box: tuple[int, int, int, int]
    bet_box: tuple[int, int, int, int]


def _runs_from_mask(mask: list[bool], min_len: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, ok in enumerate(mask):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            if i - start >= min_len:
                runs.append((start, i))
            start = None
    if start is not None and len(mask) - start >= min_len:
        runs.append((start, len(mask)))
    return runs


def _edge_projection(gray: Image.Image, roi: tuple[int, int, int, int]) -> tuple[list[int], list[int]]:
    x0, y0, x1, y1 = roi
    edge = gray.filter(ImageFilter.FIND_EDGES)
    px = edge.load()

    cols: list[int] = []
    for x in range(x0, x1):
        s = 0
        for y in range(y0, y1):
            s += int(px[x, y])
        cols.append(s)

    rows: list[int] = []
    for y in range(y0, y1):
        s = 0
        for x in range(x0, x1):
            s += int(px[x, y])
        rows.append(s)

    return cols, rows


def _pick_peak_lines(scores: list[int], min_gap: int, take: int) -> list[int]:
    peaks: list[tuple[int, int]] = []
    for i in range(1, len(scores) - 1):
        if scores[i] > scores[i - 1] and scores[i] >= scores[i + 1]:
            peaks.append((scores[i], i))
    peaks.sort(reverse=True)

    selected: list[int] = []
    for _, idx in peaks:
        if all(abs(idx - prev) >= min_gap for prev in selected):
            selected.append(idx)
        if len(selected) >= take:
            break
    return sorted(selected)


def _detect_reel_boxes(face_rgba: Image.Image) -> list[tuple[int, int, int, int]]:
    w, h = face_rgba.size
    alpha = face_rgba.getchannel("A")
    px = alpha.load()

    y0 = int(h * 0.22)
    y1 = int(h * 0.50)
    x0 = int(w * 0.08)
    x1 = int(w * 0.92)
    roi_h = max(1, y1 - y0)

    col_transparent: list[int] = []
    for x in range(x0, x1):
        c = 0
        for y in range(y0, y1):
            if px[x, y] < 10:
                c += 1
        col_transparent.append(c)

    col_mask = [c >= int(roi_h * 0.45) for c in col_transparent]
    x_runs = _runs_from_mask(col_mask, min_len=max(10, w // 30))
    if len(x_runs) < 3:
        raise RuntimeError(f"Unable to detect 3 transparent reel windows in {DEFAULT_FACE_PATH}")

    # Keep 3 widest runs.
    x_runs = sorted(x_runs, key=lambda r: r[1] - r[0], reverse=True)[:3]
    x_runs = sorted(x_runs, key=lambda r: r[0])

    boxes: list[tuple[int, int, int, int]] = []
    for rx0, rx1 in x_runs:
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
        y_runs = _runs_from_mask(row_mask, min_len=max(12, h // 18))
        if not y_runs:
            raise RuntimeError("Unable to detect transparent reel window height")
        ry0, ry1 = max(y_runs, key=lambda r: r[1] - r[0])
        boxes.append((wx0, y0 + ry0, wx1, y0 + ry1))

    return boxes


def _detect_paytable_layout(face_rgba: Image.Image) -> tuple[
    list[tuple[int, int, int, int]],
    list[list[tuple[int, int, int, int]]],
    tuple[int, int, int, int],
    tuple[int, int, int, int],
]:
    w, h = face_rgba.size
    gray = face_rgba.convert("L")

    grid_roi = (int(w * 0.24), int(h * 0.42), int(w * 0.77), int(h * 0.81))
    col_scores, row_scores = _edge_projection(gray, grid_roi)

    v_local = _pick_peak_lines(col_scores, min_gap=max(16, w // 36), take=6)
    if len(v_local) < 4:
        raise RuntimeError("Unable to detect paytable column boundaries")
    # Prefer middle 4 peaks for the 3 payout columns.
    if len(v_local) > 4:
        mid_start = (len(v_local) - 4) // 2
        v_local = v_local[mid_start : mid_start + 4]
    gx0 = grid_roi[0]
    v_lines = [gx0 + v for v in v_local]

    y_local = _pick_peak_lines(row_scores, min_gap=max(12, h // 110), take=14)
    y_abs = sorted(grid_roi[1] + y for y in y_local if (grid_roi[1] + y) >= int(h * 0.47))
    row_lines: list[int] = []
    for y in y_abs:
        if not row_lines or y - row_lines[-1] >= max(12, h // 120):
            row_lines.append(y)
    if len(row_lines) < 8:
        raise RuntimeError("Unable to detect paytable row boundaries")
    row_lines = row_lines[:8]

    header_boxes = [(v_lines[c], row_lines[0], v_lines[c + 1], row_lines[1]) for c in range(3)]
    cell_boxes = [
        [(v_lines[c], row_lines[r + 1], v_lines[c + 1], row_lines[r + 2]) for c in range(3)]
        for r in range(6)
    ]

    wide_roi = (int(w * 0.16), row_lines[0], int(w * 0.86), row_lines[1])
    wide_cols, _ = _edge_projection(gray, wide_roi)
    wx0 = wide_roi[0]
    left_idx_end = max(1, v_lines[0] - wx0)
    right_idx_start = max(0, v_lines[-1] - wx0)

    left_candidates = sorted(
        range(left_idx_end), key=lambda i: wide_cols[i], reverse=True
    )[:10]
    left_pick = min(left_candidates, key=lambda i: abs((wx0 + i) - v_lines[0])) if left_candidates else 0
    for i in left_candidates:
        if (v_lines[0] - (wx0 + i)) >= max(12, w // 60):
            left_pick = i
            break

    right_band_len = len(wide_cols) - right_idx_start
    right_candidates = sorted(
        range(right_band_len), key=lambda i: wide_cols[right_idx_start + i], reverse=True
    )[:10]
    right_pick = right_candidates[0] if right_candidates else 0
    for i in right_candidates:
        if ((wx0 + right_idx_start + i) - v_lines[-1]) >= max(12, w // 60):
            right_pick = i
            break

    outer_left = wx0 + left_pick
    outer_right = wx0 + right_idx_start + right_pick

    balance_box = (outer_left, row_lines[0], v_lines[0], row_lines[1])
    bet_box = (v_lines[-1], row_lines[0], outer_right, row_lines[1])
    return header_boxes, cell_boxes, balance_box, bet_box


@lru_cache(maxsize=8)
def _layout_for_size(size: tuple[int, int]) -> _OverlayLayout:
    face = Image.open(DEFAULT_FACE_PATH).convert("RGBA")
    if face.size != size:
        face = face.resize(size, Image.Resampling.LANCZOS)

    reel_boxes = _detect_reel_boxes(face)
    header_boxes, cell_boxes, balance_box, bet_box = _detect_paytable_layout(face)
    return _OverlayLayout(reel_boxes, header_boxes, cell_boxes, balance_box, bet_box)


def _layout_for_face(face_rgba: Image.Image) -> _OverlayLayout:
    return _layout_for_size(face_rgba.size)


@lru_cache(maxsize=32)
def _font(size: int, font_path: str | None) -> ImageFont.ImageFont:
    paths: list[Path] = []
    if font_path:
        paths.append(Path(font_path))
    paths.extend(
        [ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf", ROOT / "assets" / "DejaVuSans-Bold.ttf"]
    )
    for path in paths:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _draw_centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.ImageFont) -> None:
    x0, y0, x1, y1 = box
    draw.text(
        ((x0 + x1) / 2, (y0 + y1) / 2),
        text,
        font=font,
        fill=(245, 248, 255, 255),
        anchor="mm",
        stroke_width=2,
        stroke_fill=(12, 16, 22, 255),
    )


def draw_overlay(
    face_rgba: Image.Image,
    payouts: list[list[int]],
    balance_text: str,
    bet_text: str,
    font_path: Path | None = None,
) -> Image.Image:
    out = face_rgba.copy().convert("RGBA")
    layout = _layout_for_face(out)
    draw = ImageDraw.Draw(out)

    hdr_font = _font(max(16, out.height // 36), str(font_path) if font_path else None)
    cell_font = _font(max(14, out.height // 42), str(font_path) if font_path else None)
    side_font = _font(max(14, out.height // 38), str(font_path) if font_path else None)

    for i, box in enumerate(layout.header_boxes):
        _draw_centered(draw, box, str(i + 1), hdr_font)

    for r in range(min(6, len(payouts))):
        for c in range(min(3, len(payouts[r]))):
            _draw_centered(draw, layout.cell_boxes[r][c], f"{int(payouts[r][c]):,}", cell_font)

    _draw_centered(draw, layout.balance_box, balance_text, side_font)
    _draw_centered(draw, layout.bet_box, bet_text, side_font)
    return out
