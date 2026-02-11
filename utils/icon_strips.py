from __future__ import annotations

import io
from typing import Iterable

import aiohttp
from PIL import Image, ImageDraw, ImageFont


def _normalize_image_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"https://www.torn.com{value}"
    return value


async def _fetch_image_bytes(session: aiohttp.ClientSession, url: str) -> bytes | None:
    normalized = _normalize_image_url(url)
    if not normalized:
        return None
    try:
        async with session.get(normalized, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception:
        return None


async def build_icon_strip_file(
    entries: Iterable[dict],
    filename: str,
    icon_size: int = 36,
    max_width: int = 700,
) -> io.BytesIO | None:
    rows = [entry for entry in entries if entry.get("image_url") and int(entry.get("quantity", 0)) > 0]
    if not rows:
        return None

    own_session = False
    session = None
    try:
        session = aiohttp.ClientSession()
        own_session = True

        prepared = []
        font = ImageFont.load_default()
        padding = 10
        x = padding
        y = padding
        line_height = icon_size + 14
        total_height = line_height + (padding * 2)

        for row in rows:
            content = await _fetch_image_bytes(session, str(row["image_url"]))
            if not content:
                continue
            try:
                icon = Image.open(io.BytesIO(content)).convert("RGBA").resize((icon_size, icon_size))
            except Exception:
                continue

            qty_text = f"×{int(row['quantity'])}"
            text_w = int(font.getlength(qty_text)) + 8
            slot_width = icon_size + text_w + 18

            if x + slot_width > max_width:
                x = padding
                y += line_height
                total_height += line_height

            prepared.append((icon, qty_text, x, y))
            x += slot_width

        if not prepared:
            return None

        image = Image.new("RGBA", (max_width, total_height), (22, 24, 31, 255))
        draw = ImageDraw.Draw(image)

        for icon, qty_text, px, py in prepared:
            image.paste(icon, (px, py), icon)
            draw.text((px + icon_size + 6, py + (icon_size // 4)), qty_text, fill=(245, 246, 248, 255), font=font)

        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        output.name = filename
        return output
    finally:
        if own_session and session:
            await session.close()
