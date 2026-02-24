from __future__ import annotations

BASE_FACE_W = 1024
BASE_FACE_H = 1536

BASE_REEL_BOXES = [
    (101, 398, 349, 612),
    (392, 398, 630, 612),
    (673, 398, 924, 612),
]


def scale_box(
    box: tuple[int, int, int, int], face_w: int, face_h: int
) -> tuple[int, int, int, int]:
    sx = float(face_w) / float(BASE_FACE_W)
    sy = float(face_h) / float(BASE_FACE_H)
    x0, y0, x1, y1 = box
    return (
        int(round(x0 * sx)),
        int(round(y0 * sy)),
        int(round(x1 * sx)),
        int(round(y1 * sy)),
    )


def scale_boxes(
    boxes: list[tuple[int, int, int, int]], face_w: int, face_h: int
) -> list[tuple[int, int, int, int]]:
    return [scale_box(box, face_w, face_h) for box in boxes]


def get_scaled_reel_boxes(face_w: int, face_h: int) -> list[tuple[int, int, int, int]]:
    return scale_boxes(BASE_REEL_BOXES, face_w, face_h)
