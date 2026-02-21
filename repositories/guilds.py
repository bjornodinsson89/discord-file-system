from __future__ import annotations

from .base import RepositoryBase


class GuildsRepository(RepositoryBase):
    async def get_settings(self, guild_id: int):
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM public.guild_settings WHERE guild_id = $1", guild_id)
            return dict(row) if row else None
