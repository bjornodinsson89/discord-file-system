from __future__ import annotations

BASE_FACE_W = 1024
BASE_FACE_H = 1536

BASE_REEL_BOXES = [
    (101, 398, 349, 612),
    (392, 398, 630, 612),
    (673, 398, 924, 612),
]

BASE_PAY_HEADERS = [
    ("1", (330, 669, 453, 725)),
    ("2", (453, 669, 569, 725)),
    ("3", (569, 669, 695, 725)),
]

BASE_PAY_CELLS = [
    [(330, 725, 453, 783), (453, 725, 569, 783), (569, 725, 695, 783)],
    [(330, 783, 453, 843), (453, 783, 569, 843), (569, 783, 695, 843)],
    [(330, 843, 453, 903), (453, 843, 569, 903), (569, 843, 695, 903)],
    [(330, 903, 453, 964), (453, 903, 569, 964), (569, 903, 695, 964)],
    [(330, 964, 453, 1023), (453, 964, 569, 1023), (569, 964, 695, 1023)],
    [(330, 1023, 453, 1080), (453, 1023, 569, 1080), (569, 1023, 695, 1080)],
]

# Derived from the paytable grid extents and header band dimensions.
BASE_BALANCE_BOX = (330 - 123, 669, 330, 725)
BASE_BET_BOX = (695, 669, 695 + 123, 725)


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


def get_scaled_layout(face_w: int, face_h: int) -> dict[str, object]:
    reel_boxes = scale_boxes(BASE_REEL_BOXES, face_w, face_h)
    pay_headers = [(label, scale_box(box, face_w, face_h)) for label, box in BASE_PAY_HEADERS]
    pay_cells = [scale_boxes(row, face_w, face_h) for row in BASE_PAY_CELLS]
    balance_box = scale_box(BASE_BALANCE_BOX, face_w, face_h)
    bet_box = scale_box(BASE_BET_BOX, face_w, face_h)
    return {
        "reel_boxes": reel_boxes,
        "pay_headers": pay_headers,
        "pay_cells": pay_cells,
        "balance_box": balance_box,
        "bet_box": bet_box,
    }
