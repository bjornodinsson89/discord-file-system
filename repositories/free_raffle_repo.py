from __future__ import annotations

from datetime import datetime

from .base import RepositoryBase


class FreeRaffleRepository(RepositoryBase):
    async def create_raffle(
        self,
        *,
        guild_id: int,
        channel_id: int,
        host_discord_id: int,
        prize_text: str,
        note_text: str | None,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO free_raffles (
                    guild_id,
                    channel_id,
                    host_discord_id,
                    prize_text,
                    note_text,
                    status
                )
                VALUES ($1, $2, $3, $4, $5, 'active')
                RETURNING *
                """,
                guild_id,
                channel_id,
                host_discord_id,
                prize_text,
                note_text,
            )
            return dict(row)

    async def set_message_id(self, raffle_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE free_raffles
                SET message_id = $2,
                    updated_at = NOW()
                WHERE id = $1
                """,
                raffle_id,
                message_id,
            )

    async def get_raffle(self, raffle_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM free_raffles WHERE id = $1",
                raffle_id,
            )
            return dict(row) if row else None

    async def set_status(self, raffle_id: int, status: str, ended_at: datetime | None = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE free_raffles
                SET status = $2,
                    ended_at = $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                raffle_id,
                status,
                ended_at,
            )

    async def add_entry(self, raffle_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO free_raffle_entries (raffle_id, discord_id)
                VALUES ($1, $2)
                ON CONFLICT (raffle_id, discord_id) DO NOTHING
                """,
                raffle_id,
                discord_id,
            )
            return result.endswith("1")

    async def get_entry_count(self, raffle_id: int) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM free_raffle_entries WHERE raffle_id = $1",
                raffle_id,
            )
            return int(value or 0)

    async def list_entry_ids(self, raffle_id: int) -> list[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT discord_id FROM free_raffle_entries WHERE raffle_id = $1",
                raffle_id,
            )
            return [int(row["discord_id"]) for row in rows]

    async def create_winner(self, raffle_id: int, discord_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO free_raffle_winners (raffle_id, discord_id)
                VALUES ($1, $2)
                ON CONFLICT (raffle_id) DO UPDATE
                SET discord_id = EXCLUDED.discord_id,
                    created_at = NOW()
                """,
                raffle_id,
                discord_id,
            )

    async def get_winner(self, raffle_id: int) -> int | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT discord_id FROM free_raffle_winners WHERE raffle_id = $1",
                raffle_id,
            )
            return int(value) if value is not None else None

    async def list_active_raffles(self, guild_id: int | None = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if guild_id is None:
                rows = await conn.fetch(
                    "SELECT * FROM free_raffles WHERE status = 'active' AND message_id IS NOT NULL"
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM free_raffles
                    WHERE status = 'active'
                      AND message_id IS NOT NULL
                      AND guild_id = $1
                    """,
                    guild_id,
                )
            return [dict(row) for row in rows]
