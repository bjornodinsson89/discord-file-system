from __future__ import annotations

import io
import random
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

CACHE_DIR = Path("data/casino_item_cache")
_MEMORY_CACHE: dict[int, Image.Image] = {}


class SlotsRenderError(Exception):
    pass


async def fetch_item_image(item_id: int) -> bytes:
    url = f"https://www.torn.com/images/items/{int(item_id)}/small.png"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise SlotsRenderError(f"Failed to fetch item image {item_id}: HTTP {resp.status}")
            return await resp.read()


async def get_item_image_small(item_id: int) -> Image.Image:
    item_id = int(item_id)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if item_id in _MEMORY_CACHE:
        return _MEMORY_CACHE[item_id].copy()

    disk_path = CACHE_DIR / f"{item_id}_small.png"
    if disk_path.exists():
        img = Image.open(disk_path).convert("RGBA")
        _MEMORY_CACHE[item_id] = img.copy()
        return img

    content = await fetch_item_image(item_id)
    disk_path.write_bytes(content)
    try:
        img = Image.open(io.BytesIO(content)).convert("RGBA")
    except Exception as exc:
        raise SlotsRenderError(f"Invalid image content for item {item_id}") from exc
    _MEMORY_CACHE[item_id] = img.copy()
    return img


async def render_slots_png(
    reels: list[int],
    bet: int,
    payout: int,
    balance: int,
    pool_tokens: int,
    pool_millis: int,
    win_label: str,
    spin_mask: list[bool] | None = None,
    spin_symbols: list[list[int]] | None = None,
) -> bytes:
    try:
        w, h = 520, 260
        canvas = Image.new("RGBA", (w, h), (22, 23, 30, 255))
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default()

        draw.rounded_rectangle(
            (10, 10, w - 10, h - 10),
            radius=18,
            fill=(36, 38, 50, 255),
            outline=(95, 100, 130, 255),
            width=3,
        )

        draw.text((24, 20), "Jumper Slots", font=font, fill=(245, 245, 245, 255))
        draw.text((24, 44), f"Result: {win_label}", font=font, fill=(205, 210, 225, 255))

        reel_top, reel_h, reel_w = 78, 96, 140
        start_x = 26
        spin_mask = list(spin_mask or [False, False, False])[:3]
        while len(spin_mask) < 3:
            spin_mask.append(False)

        for idx, item_id in enumerate(reels):
            x0 = start_x + idx * (reel_w + 20)
            x1 = x0 + reel_w
            y0 = reel_top
            y1 = y0 + reel_h
            draw.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=12,
                fill=(16, 17, 24, 255),
                outline=(115, 121, 154, 255),
                width=2,
            )
            if spin_mask[idx]:
                reel_layer = Image.new("RGBA", (reel_w, reel_h), (0, 0, 0, 0))
                reel_symbols = [int(item_id)]
                if spin_symbols and idx < len(spin_symbols):
                    reel_symbols.extend(int(v) for v in (spin_symbols[idx] or [])[:2])
                while len(reel_symbols) < 3:
                    reel_symbols.append(int(item_id))

                offsets = (-10, 0, 10)
                alphas = (110, 255, 110)
                for sym_id, offset, alpha in zip(reel_symbols[:3], offsets, alphas, strict=False):
                    img = await get_item_image_small(sym_id)
                    fitted = img.copy()
                    fitted.thumbnail((76, 76))
                    if alpha < 255:
                        alpha_channel = fitted.getchannel("A")
                        alpha_channel = alpha_channel.point(lambda p: (p * alpha) // 255)
                        fitted.putalpha(alpha_channel)
                    px = (reel_w - fitted.width) // 2
                    py = (reel_h - fitted.height) // 2 + offset
                    reel_layer.alpha_composite(fitted, (px, py))

                if random.random() < 0.65:
                    reel_layer = reel_layer.filter(ImageFilter.GaussianBlur(radius=1))
                canvas.alpha_composite(reel_layer, (x0, y0))
            else:
                img = await get_item_image_small(int(item_id))
                fitted = img.copy()
                fitted.thumbnail((76, 76))
                px = x0 + (reel_w - fitted.width) // 2
                py = y0 + (reel_h - fitted.height) // 2
                canvas.alpha_composite(fitted, (px, py))

        pool_value = f"{pool_tokens}.{pool_millis:03d}"
        draw.text((24, 192), f"Bet: {bet}", font=font, fill=(240, 240, 240, 255))
        draw.text((130, 192), f"Payout: {payout}", font=font, fill=(240, 240, 240, 255))
        draw.text((260, 192), f"Balance: {balance}", font=font, fill=(240, 240, 240, 255))
        draw.text((24, 216), f"Jackpot Pool: {pool_value}", font=font, fill=(248, 214, 102, 255))

        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except SlotsRenderError:
        raise
    except Exception as exc:
        raise SlotsRenderError("Unable to render slots image") from exc
