from __future__ import annotations

from utils.database import get_database


def normalize_combo(reels: list[int]) -> str:
    normalized = [int(v) for v in reels][:3]
    while len(normalized) < 3:
        normalized.append(0)
    return f"{normalized[0]},{normalized[1]},{normalized[2]}"


async def get_slot_asset_url(combo: str) -> str | None:
    db = get_database()
    async with db.acquire(operation="slot_assets_get_url") as conn:
        row = await conn.fetchrow(
            "SELECT url FROM slot_assets WHERE combo = $1",
            str(combo),
        )
    if not row:
        return None
    value = row.get("url")
    return str(value) if value else None


async def upsert_slot_asset(
    combo: str,
    url: str,
    message_id: int,
    frames: int = 40,
    duration_ms: int = 110,
) -> None:
    db = get_database()
    async with db.acquire(operation="slot_assets_upsert") as conn:
        await conn.execute(
            """
            INSERT INTO slot_assets (combo, url, message_id, frames, duration_ms)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (combo)
            DO UPDATE SET
                url = EXCLUDED.url,
                message_id = EXCLUDED.message_id,
                frames = EXCLUDED.frames,
                duration_ms = EXCLUDED.duration_ms
            """,
            str(combo),
            str(url),
            int(message_id),
            int(frames),
            int(duration_ms),
        )


async def has_combo(combo: str) -> bool:
    db = get_database()
    async with db.acquire(operation="slot_assets_has_combo") as conn:
        value = await conn.fetchval(
            "SELECT 1 FROM slot_assets WHERE combo = $1",
            str(combo),
        )
    return value is not None


async def list_missing_combos(all_combos: list[str]) -> list[str]:
    if not all_combos:
        return []
    db = get_database()
    async with db.acquire(operation="slot_assets_list_missing") as conn:
        rows = await conn.fetch(
            "SELECT combo FROM slot_assets WHERE combo = ANY($1::text[])",
            [str(combo) for combo in all_combos],
        )
    existing = {str(row["combo"]) for row in rows}
    return [combo for combo in all_combos if combo not in existing]
