from __future__ import annotations

from .base import RepositoryBase


class UsersRepository(RepositoryBase):
    async def get_user_api_key(self, discord_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_api_keys WHERE discord_id = $1",
                discord_id,
            )
            return dict(row) if row else None
