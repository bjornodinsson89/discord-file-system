from __future__ import annotations

import io
import logging
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "casino_items"
BANNER_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "banners" / "jump_slots_banner.png"
)
_MEMORY_CACHE: dict[int, Image.Image] = {}
_BANNER_CACHE: Image.Image | None = None
log = logging.getLogger("happy_jumper.casino.slots.render")


class SlotsRenderError(Exception):
    pass


def _get_banner() -> Image.Image | None:
    global _BANNER_CACHE
    if _BANNER_CACHE is not None:
        return _BANNER_CACHE.copy()
    if not BANNER_PATH.exists():
        return None
    img = Image.open(BANNER_PATH).convert("RGBA")
    _BANNER_CACHE = img
    return img.copy()


async def get_item_image_small(item_id: int) -> Image.Image | None:
    item_id = int(item_id)
    if item_id in _MEMORY_CACHE:
        return _MEMORY_CACHE[item_id].copy()

    asset_path = ASSET_DIR / f"{item_id}.png"
    if not asset_path.exists():
        log.debug("slots.icon_missing item_id=%s path=%s", item_id, asset_path)
        return None

    try:
        img = Image.open(asset_path).convert("RGBA")
    except Exception:
        return None

    _MEMORY_CACHE[item_id] = img.copy()
    return img


async def render_slots_png(
    reels: list[int],
    bet: int,
    payout: int,
    balance: int | None,
    pool_tokens: int,
    pool_millis: int,
    status_text: str,
    spin_mask: list[bool] | None = None,
) -> bytes:
    image = await render_slots_frame_image(
        reels=reels,
        bet=bet,
        payout=payout,
        balance=balance,
        pool_tokens=pool_tokens,
        pool_millis=pool_millis,
        status_text=status_text,
        spin_mask=spin_mask,
    )
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def render_slots_frame_image(
    reels: list[int],
    bet: int,
    payout: int,
    balance: int | None,
    pool_tokens: int,
    pool_millis: int,
    status_text: str,
    spin_mask: list[bool] | None = None,
) -> Image.Image:
    try:
        w, h = 820, 420
        banner_h = 140
        canvas = Image.new("RGBA", (w, h), (18, 19, 27, 255))
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default()

        draw.rounded_rectangle(
            (8, 8, w - 8, h - 8),
            radius=18,
            fill=(31, 34, 46, 255),
            outline=(95, 100, 130, 255),
            width=2,
        )

        banner = _get_banner()
        if banner is not None:
            ratio = w / banner.width
            resized_h = max(banner_h, int(banner.height * ratio))
            banner = banner.resize((w, resized_h), Image.Resampling.LANCZOS)
            crop_top = max(0, (resized_h - banner_h) // 2)
            banner = banner.crop((0, crop_top, w, crop_top + banner_h))
            canvas.alpha_composite(banner, (0, 0))
        else:
            draw.rectangle((0, 0, w, banner_h), fill=(34, 36, 52, 255))
            draw.text((w // 2 - 34, 58), "JUMP SLOTS", font=font, fill=(246, 246, 246, 255))

        reel_top = banner_h + 18
        reel_h = 170
        reel_w = w - 80
        reel_left = 40
        reel_bottom = reel_top + reel_h

        draw.rounded_rectangle(
            (reel_left, reel_top, reel_left + reel_w, reel_bottom),
            radius=16,
            fill=(16, 17, 24, 255),
            outline=(125, 132, 160, 255),
            width=3,
        )

        cell_w = reel_w // 3
        for idx in (1, 2):
            x = reel_left + idx * cell_w
            draw.line((x, reel_top + 8, x, reel_bottom - 8), fill=(110, 116, 148, 255), width=2)

        spin_mask = list(spin_mask or [False, False, False])[:3]
        while len(spin_mask) < 3:
            spin_mask.append(False)

        for idx, item_id in enumerate(reels[:3]):
            cell_left = reel_left + idx * cell_w

            base_img = await get_item_image_small(int(item_id))

            if spin_mask[idx]:
                reel_layer = Image.new("RGBA", (cell_w, reel_h), (0, 0, 0, 0))
                reel_symbols = [int(item_id), random.choice(reels), random.choice(reels)]
                offsets = (-26, 0, 26)
                alphas = (100, 230, 100)
                for sym_id, offset, alpha in zip(reel_symbols[:3], offsets, alphas, strict=False):
                    img = await get_item_image_small(sym_id)
                    if img is None:
                        continue
                    fitted = img.copy()
                    fitted.thumbnail((80, 80))
                    if alpha < 255:
                        alpha_channel = fitted.getchannel("A")
                        alpha_channel = alpha_channel.point(lambda p, a=alpha: (p * a) // 255)
                        fitted.putalpha(alpha_channel)
                    px = (cell_w - fitted.width) // 2
                    py = (reel_h - fitted.height) // 2 + offset
                    reel_layer.alpha_composite(fitted, (px, py))

                reel_layer = reel_layer.filter(ImageFilter.GaussianBlur(radius=1))
                canvas.alpha_composite(reel_layer, (cell_left, reel_top))
            else:
                if base_img is not None:
                    fitted = base_img.copy()
                    fitted.thumbnail((80, 80))
                    px = cell_left + (cell_w - fitted.width) // 2
                    py = reel_top + (reel_h - fitted.height) // 2
                    canvas.alpha_composite(fitted, (px, py))

            if base_img is None:
                ph_w, ph_h = 96, 96
                ph_x = cell_left + (cell_w - ph_w) // 2
                ph_y = reel_top + (reel_h - ph_h) // 2
                draw.rounded_rectangle(
                    (ph_x, ph_y, ph_x + ph_w, ph_y + ph_h),
                    radius=12,
                    fill=(46, 49, 67, 255),
                    outline=(153, 158, 183, 255),
                    width=2,
                )
                item_label = str(item_id)
                draw.text((ph_x + 10, ph_y + 42), item_label, font=font, fill=(238, 238, 238, 255))

        footer_top = reel_bottom + 16
        pool_value = f"{pool_tokens}.{pool_millis:03d}"
        net = payout - bet
        draw.text((40, footer_top), f"Status: {status_text}", font=font, fill=(240, 240, 240, 255))
        draw.text((40, footer_top + 22), f"Bet: {bet}", font=font, fill=(240, 240, 240, 255))
        draw.text((180, footer_top + 22), f"Payout: {payout}", font=font, fill=(240, 240, 240, 255))
        draw.text((340, footer_top + 22), f"Net: {net}", font=font, fill=(240, 240, 240, 255))
        if balance is None:
            draw.text((460, footer_top + 22), "Balance: ...", font=font, fill=(210, 210, 210, 255))
        else:
            draw.text(
                (460, footer_top + 22), f"Balance: {balance}", font=font, fill=(240, 240, 240, 255)
            )
        draw.text(
            (40, footer_top + 44), f"Pool: {pool_value}", font=font, fill=(248, 214, 102, 255)
        )

        return canvas
    except SlotsRenderError:
        raise
    except Exception as exc:
        raise SlotsRenderError("Unable to render slots image") from exc


async def render_slots_gif(
    frames: list[dict],
    frame_delay_ms: int,
) -> bytes:
    if not frames:
        raise SlotsRenderError("No frames provided for GIF render")

    try:
        frames_p: list[Image.Image] = []
        for frame in frames:
            image = await render_slots_frame_image(
                reels=frame["reels"],
                bet=int(frame["bet"]),
                payout=int(frame["payout"]),
                balance=(None if frame.get("balance") is None else int(frame["balance"])),
                pool_tokens=int(frame["pool_tokens"]),
                pool_millis=int(frame["pool_millis"]),
                status_text=str(frame["status_text"]),
                spin_mask=frame.get("spin_mask"),
            )
            image = image.convert("RGBA")
            frame_p = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
            frames_p.append(frame_p)

        out = io.BytesIO()
        frames_p[0].save(
            out,
            format="GIF",
            save_all=True,
            append_images=frames_p[1:],
            duration=frame_delay_ms,
            loop=0,
            disposal=2,
            optimize=True,
        )
        return out.getvalue()
    except Exception as exc:
        raise SlotsRenderError("Unable to render slots GIF") from exc
