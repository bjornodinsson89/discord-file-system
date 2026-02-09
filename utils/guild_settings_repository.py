"""Repository helpers for guild settings CRUD operations."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


class GuildSettingsRepository:
    """CRUD helpers with safe upserts for per-guild settings."""

    def __init__(self, db_manager):
        self._db = db_manager

    @staticmethod
    def _normalize_admin_role_ids(admin_role_ids: Optional[Iterable[Any]]) -> Optional[list[str]]:
        if admin_role_ids is None:
            return None
        normalized: list[str] = []
        for role_id in admin_role_ids:
            if role_id is None:
                continue
            normalized.append(str(int(role_id)))
        return normalized

    async def get_or_create(self, guild_id: int) -> Dict[str, Any]:
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
                guild_id,
            )
            row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
            return dict(row)

    async def upsert(
        self,
        guild_id: int,
        announce_channel_id: Optional[int] = None,
        admin_role_ids: Optional[Iterable[Any]] = None,
        welcome_enabled: Optional[bool] = None,
        welcome_message_template: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_roles = self._normalize_admin_role_ids(admin_role_ids)
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO guild_settings (
                    guild_id,
                    announce_channel_id,
                    admin_role_ids,
                    welcome_enabled,
                    welcome_message_template
                ) VALUES ($1, $2, $3::jsonb, COALESCE($4, FALSE), $5)
                ON CONFLICT (guild_id) DO UPDATE SET
                    announce_channel_id = COALESCE(EXCLUDED.announce_channel_id, guild_settings.announce_channel_id),
                    admin_role_ids = COALESCE(EXCLUDED.admin_role_ids, guild_settings.admin_role_ids),
                    welcome_enabled = COALESCE($4, guild_settings.welcome_enabled),
                    welcome_message_template = COALESCE($5, guild_settings.welcome_message_template),
                    updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                announce_channel_id,
                normalized_roles,
                welcome_enabled,
                welcome_message_template,
            )
            return dict(row)

    async def set_announce_channel(self, guild_id: int, announce_channel_id: int) -> Dict[str, Any]:
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO guild_settings (guild_id, announce_channel_id)
                VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET
                    announce_channel_id = EXCLUDED.announce_channel_id,
                    updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                announce_channel_id,
            )
            return dict(row)
