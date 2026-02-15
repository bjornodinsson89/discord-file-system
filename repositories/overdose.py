from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from .base import RepositoryBase


log = logging.getLogger("happy_jumper.overdose_repo")

_OD_MIGRATION_HINT = (
    "Missing overdose schema/table. Run manual SQL for public.overdose_events and "
    "jump_99k_signups overdose_* columns before starting bot."
)


class OverdoseRepository(RepositoryBase):
    async def insert_event_if_new(
        self,
        *,
        guild_id: int,
        discord_id: int,
        torn_user_id: int | None,
        event_type: str,
        event_timestamp: int,
        torn_log_id: str,
        meta: dict,
    ) -> bool:
        async with self.pool.acquire() as conn:
            try:
                result = await conn.execute(
                    """
                    INSERT INTO public.overdose_events (
                        guild_id, discord_id, torn_user_id, event_type,
                        event_timestamp, torn_log_id, meta, created_at
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,NOW())
                    ON CONFLICT (torn_log_id) DO NOTHING
                    """,
                    guild_id,
                    discord_id,
                    torn_user_id,
                    event_type,
                    event_timestamp,
                    str(torn_log_id),
                    json.dumps(meta or {}, separators=(",", ":"), ensure_ascii=False),
                )
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_OD_MIGRATION_HINT)
                return False
            return str(result).endswith("1")

    async def get_latest_for_user(self, *, guild_id: int, discord_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT guild_id, discord_id, torn_user_id, event_type,
                           event_timestamp, torn_log_id, meta, created_at
                    FROM public.overdose_events
                    WHERE guild_id=$1 AND discord_id=$2
                    ORDER BY event_timestamp DESC
                    LIMIT 1
                    """,
                    guild_id,
                    discord_id,
                )
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_OD_MIGRATION_HINT)
                return None
            return dict(row) if row else None
