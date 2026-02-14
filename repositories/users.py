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


    async def upsert_user_api_key(self, *, discord_id: int, torn_user_id: int, encrypted_key: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_api_keys (discord_id, torn_user_id, encrypted_key, created_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (discord_id)
                DO UPDATE SET
                    torn_user_id = EXCLUDED.torn_user_id,
                    encrypted_key = EXCLUDED.encrypted_key,
                    updated_at = NOW()
                """,
                discord_id,
                torn_user_id,
                encrypted_key,
            )

    async def delete_user_api_key(self, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM user_api_keys WHERE discord_id = $1 RETURNING discord_id",
                discord_id,
            )
            return row is not None
