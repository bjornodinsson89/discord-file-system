from __future__ import annotations

import re

from .base import RepositoryBase


_CURLY_QUOTES_RE = re.compile(r"[’‘]")
_NON_ALNUM_WS_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


def norm_name(s: str) -> str:
    value = (s or "").strip().lower()
    value = _CURLY_QUOTES_RE.sub("'", value)
    value = _NON_ALNUM_WS_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).strip()
    return value


class TornItemsRepository(RepositoryBase):
    async def upsert_items(self, rows: list[tuple[int, str, str, str]]) -> int:
        if not rows:
            return 0

        item_ids = [row[0] for row in rows]
        names = [row[1] for row in rows]
        norm_names = [row[2] for row in rows]
        image_urls = [row[3] for row in rows]

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO torn_items(item_id, name, norm_name, image_url)
                SELECT *
                FROM unnest($1::int[], $2::text[], $3::text[], $4::text[])
                ON CONFLICT (item_id) DO UPDATE
                SET name = EXCLUDED.name,
                    norm_name = EXCLUDED.norm_name,
                    image_url = EXCLUDED.image_url,
                    updated_at = NOW()
                """,
                item_ids,
                names,
                norm_names,
                image_urls,
            )
        return len(rows)

    async def upsert_aliases(self, aliases: dict[str, int]) -> int:
        if not aliases:
            return 0

        alias_norms = list(aliases.keys())
        item_ids = [aliases[alias] for alias in alias_norms]

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO torn_item_aliases(alias_norm, item_id)
                SELECT *
                FROM unnest($1::text[], $2::int[])
                ON CONFLICT (alias_norm) DO UPDATE
                SET item_id = EXCLUDED.item_id
                """,
                alias_norms,
                item_ids,
            )
        return len(aliases)

    async def set_last_refresh_iso(self, iso: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO torn_items_meta(key, value, updated_at)
                VALUES ('last_refresh_iso', $1, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = NOW()
                """,
                iso,
            )

    async def resolve_item_id(self, raw_name: str) -> int:
        normalized = norm_name(raw_name)
        if not normalized:
            return 0

        async with self.pool.acquire() as conn:
            alias_match = await conn.fetchval(
                """
                SELECT item_id
                FROM torn_item_aliases
                WHERE alias_norm = $1
                """,
                normalized,
            )
            if alias_match is not None:
                return int(alias_match)

            item_match = await conn.fetchval(
                """
                SELECT item_id
                FROM torn_items
                WHERE norm_name = $1
                """,
                normalized,
            )
            if item_match is not None:
                return int(item_match)

        return 0

    async def get_item_meta(self, item_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT item_id, name, image_url
                FROM torn_items
                WHERE item_id = $1
                """,
                item_id,
            )
            return dict(row) if row else None

    async def get_item_meta_by_name(self, raw_name: str) -> dict | None:
        item_id = await self.resolve_item_id(raw_name)
        if not item_id:
            return None
        return await self.get_item_meta(item_id)

    async def get_item_by_name(self, raw_name: str) -> dict | None:
        """Backward/compat helper: resolve by item name or alias and return item metadata."""
        return await self.get_item_meta_by_name(raw_name)
