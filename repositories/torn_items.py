from __future__ import annotations

import re

from .base import RepositoryBase


_CURLY_QUOTES_RE = re.compile(r"[’‘]")
_NON_ALNUM_WS_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")
_LEADING_COUNT_RE = re.compile(r"^(?:x\s*\d+|\d+\s*x?|\d+)\s+", re.IGNORECASE)
_PART_SPLIT_RE = re.compile(r"\s*(?:\+|,|\band\b)\s*", re.IGNORECASE)


class TornItemLookupError(ValueError):
    """Raised when a Torn item name cannot be resolved deterministically."""



def norm_name(s: str) -> str:
    value = (s or "").strip().lower()
    value = _CURLY_QUOTES_RE.sub("'", value)
    value = _NON_ALNUM_WS_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).strip()
    return value




def derive_lookup_candidates(raw_name: str) -> list[str]:
    source = (raw_name or "").strip()
    if not source:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def _push(value: str) -> None:
        cleaned = value.strip(" -–—")
        normalized = norm_name(cleaned)
        if cleaned and normalized and normalized not in seen:
            candidates.append(cleaned)
            seen.add(normalized)

    _push(source)
    for part in _PART_SPLIT_RE.split(source):
        cleaned_part = part.strip()
        if not cleaned_part:
            continue
        _push(cleaned_part)
        without_count = _LEADING_COUNT_RE.sub("", cleaned_part).strip()
        if without_count and without_count != cleaned_part:
            _push(without_count)
    return candidates

class TornItemsRepository(RepositoryBase):
    async def upsert_items(self, rows: list[tuple[int, str, str, str, str | None]]) -> int:
        if not rows:
            return 0

        item_ids = [row[0] for row in rows]
        names = [row[1] for row in rows]
        norm_names = [row[2] for row in rows]
        image_urls = [row[3] for row in rows]
        descriptions = [row[4] for row in rows]

        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO torn_items(item_id, name, norm_name, image_url, description)
                SELECT *
                FROM unnest($1::int[], $2::text[], $3::text[], $4::text[], $5::text[])
                ON CONFLICT (item_id) DO UPDATE
                SET name = EXCLUDED.name,
                    norm_name = EXCLUDED.norm_name,
                    image_url = EXCLUDED.image_url,
                    description = EXCLUDED.description,
                    updated_at = NOW()
                """,
                item_ids,
                names,
                norm_names,
                image_urls,
                descriptions,
            )
        return len(rows)

    async def upsert_aliases(self, aliases: dict[str, int]) -> int:
        if not aliases:
            return 0

        alias_norms = list(aliases.keys())
        item_ids = [aliases[alias] for alias in alias_norms]

        async with self.acquire() as conn:
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
        async with self.acquire() as conn:
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

        async with self.acquire() as conn:
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
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT item_id, name, image_url, description
                FROM torn_items
                WHERE item_id = $1
                """,
                item_id,
            )
            return dict(row) if row else None

    async def resolve_store_item_match_by_name(self, raw_name: str) -> dict | None:
        lookup_name = (raw_name or "").strip()
        if not lookup_name:
            return None
        normalized = norm_name(lookup_name)
        if not normalized:
            return None

        async with self.acquire() as conn:
            exact_rows = await conn.fetch(
                """
                SELECT item_id, name, norm_name, image_url, description
                FROM torn_items
                WHERE LOWER(name) = LOWER($1)
                ORDER BY item_id ASC
                """,
                lookup_name,
            )
            if len(exact_rows) == 1:
                return dict(exact_rows[0])
            if len(exact_rows) > 1:
                raise TornItemLookupError(
                    f"Multiple Torn items match '{lookup_name}'. Please enter a more specific item name."
                )

            normalized_rows = await conn.fetch(
                """
                SELECT item_id, name, norm_name, image_url, description
                FROM torn_items
                WHERE norm_name = $1
                ORDER BY item_id ASC
                """,
                normalized,
            )
            if len(normalized_rows) == 1:
                return dict(normalized_rows[0])
            if len(normalized_rows) > 1:
                raise TornItemLookupError(
                    f"Multiple Torn items match '{lookup_name}'. Please enter a more specific item name."
                )
        return None

    async def get_item_meta_by_name(self, raw_name: str) -> dict | None:
        item_id = await self.resolve_item_id(raw_name)
        if not item_id:
            return None
        return await self.get_item_meta(item_id)

    async def get_item_by_name(self, raw_name: str) -> dict | None:
        """Backward/compat helper: resolve by item name or alias and return item metadata."""
        return await self.get_item_meta_by_name(raw_name)
