from __future__ import annotations

from typing import Any

from .base import RepositoryBase


class GuildSettingsRepository(RepositoryBase):
    async def get(self, guild_id: int) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM public.guild_settings WHERE guild_id = $1", guild_id)
            return dict(row) if row else None

    async def upsert_applications_settings(
        self,
        guild_id: int,
        applications_category_id: int | None,
        applications_admin_inbox_channel_id: int | None,
        host_apps_admin_inbox_channel_id: int | None,
        insurance_apps_admin_inbox_channel_id: int | None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.guild_settings (
                    guild_id,
                    applications_category_id,
                    applications_admin_inbox_channel_id,
                    host_apps_admin_inbox_channel_id,
                    insurance_apps_admin_inbox_channel_id
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id) DO UPDATE
                SET applications_category_id = EXCLUDED.applications_category_id,
                    applications_admin_inbox_channel_id = EXCLUDED.applications_admin_inbox_channel_id,
                    host_apps_admin_inbox_channel_id = EXCLUDED.host_apps_admin_inbox_channel_id,
                    insurance_apps_admin_inbox_channel_id = EXCLUDED.insurance_apps_admin_inbox_channel_id
                RETURNING *
                """,
                guild_id,
                applications_category_id,
                applications_admin_inbox_channel_id,
                host_apps_admin_inbox_channel_id,
                insurance_apps_admin_inbox_channel_id,
            )
            return dict(row)
