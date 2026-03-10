from __future__ import annotations

from datetime import datetime

from .base import RepositoryBase


class UserTornIdentityCacheRepository(RepositoryBase):
    async def get_identity(self, guild_id: int, discord_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM public.user_torn_identity_cache
                WHERE guild_id = $1
                  AND discord_id = $2
                """,
                int(guild_id),
                int(discord_id),
            )
        return dict(row) if row else None

    async def upsert_identity(
        self,
        guild_id: int,
        discord_id: int,
        torn_user_id: int,
        torn_name: str | None,
        source: str,
        is_official_discord_verified: bool,
        last_verified_at: datetime | None = None,
    ) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.user_torn_identity_cache (
                    guild_id,
                    discord_id,
                    torn_user_id,
                    torn_name,
                    source,
                    is_official_discord_verified,
                    last_verified_at,
                    created_at,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
                ON CONFLICT (guild_id, discord_id)
                DO UPDATE SET
                    torn_user_id = EXCLUDED.torn_user_id,
                    torn_name = EXCLUDED.torn_name,
                    source = EXCLUDED.source,
                    is_official_discord_verified = EXCLUDED.is_official_discord_verified,
                    last_verified_at = COALESCE(EXCLUDED.last_verified_at, public.user_torn_identity_cache.last_verified_at),
                    updated_at = NOW()
                """,
                int(guild_id),
                int(discord_id),
                int(torn_user_id),
                torn_name,
                source,
                bool(is_official_discord_verified),
                last_verified_at,
            )

    async def get_identity_by_torn_id(self, guild_id: int, torn_user_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM public.user_torn_identity_cache
                WHERE guild_id = $1
                  AND torn_user_id = $2
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                int(guild_id),
                int(torn_user_id),
            )
        return dict(row) if row else None
