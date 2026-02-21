from __future__ import annotations

from .base import RepositoryBase


class PoolsRepository(RepositoryBase):
    async def create_pool(
        self,
        guild_id: int,
        created_by_discord_id: int,
        ticket_price_xanax: int,
        tickets_total: int,
        max_per_user: int,
        announce_channel_id: int | None,
        panel_channel_id: int | None,
    ) -> int:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO xanax_pools (
                    guild_id,
                    created_by_discord_id,
                    status,
                    ticket_price_xanax,
                    tickets_total,
                    max_per_user,
                    announce_channel_id,
                    panel_channel_id
                ) VALUES ($1, $2, 'active', $3, $4, $5, $6, $7)
                RETURNING id
                """,
                guild_id,
                created_by_discord_id,
                ticket_price_xanax,
                tickets_total,
                max_per_user,
                announce_channel_id,
                panel_channel_id,
            )
        return int(row["id"])

    async def get_active_pool(self, guild_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM xanax_pools
                WHERE guild_id = $1
                  AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                guild_id,
            )
        return dict(row) if row else None

    async def list_active_pools(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_by_discord_id, ticket_price_xanax, tickets_total, max_per_user,
                       panel_channel_id, panel_message_id, created_at
                FROM xanax_pools
                WHERE guild_id = $1 AND status = 'active'
                ORDER BY created_at DESC
                """,
                guild_id,
            )
        return [dict(row) for row in rows]

    async def get_pool(self, pool_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM xanax_pools WHERE id = $1", pool_id)
        return dict(row) if row else None

    async def set_panel_ref(self, pool_id: int, panel_channel_id: int, panel_message_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE xanax_pools
                SET panel_channel_id = $2,
                    panel_message_id = $3
                WHERE id = $1
                """,
                pool_id,
                panel_channel_id,
                panel_message_id,
            )

    async def end_pool(self, pool_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE xanax_pools
                SET status = 'ended', ended_at = NOW()
                WHERE id = $1
                """,
                pool_id,
            )

    async def add_entry(self, pool_id: int, user_discord_id: int, tickets: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO xanax_pool_entries(pool_id, user_discord_id, tickets)
                VALUES ($1, $2, $3)
                """,
                pool_id,
                user_discord_id,
                tickets,
            )

    async def get_user_tickets(self, pool_id: int, user_discord_id: int) -> int:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(SUM(tickets), 0)
                FROM xanax_pool_entries
                WHERE pool_id = $1 AND user_discord_id = $2
                """,
                pool_id,
                user_discord_id,
            )
        return int(value or 0)

    async def get_total_tickets(self, pool_id: int) -> int:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(SUM(tickets), 0)
                FROM xanax_pool_entries
                WHERE pool_id = $1
                """,
                pool_id,
            )
        return int(value or 0)

    async def count_entries(self, pool_id: int) -> int:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM xanax_pool_entries
                WHERE pool_id = $1
                """,
                pool_id,
            )
        return int(value or 0)

    async def list_entries(self, pool_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_discord_id, COALESCE(SUM(tickets), 0) AS tickets
                FROM xanax_pool_entries
                WHERE pool_id = $1
                GROUP BY user_discord_id
                ORDER BY user_discord_id ASC
                """,
                pool_id,
            )
        return [dict(row) for row in rows]

    async def get_active_pools_with_panels(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, panel_channel_id, panel_message_id
                FROM xanax_pools
                WHERE status = 'active'
                  AND panel_channel_id IS NOT NULL
                  AND panel_message_id IS NOT NULL
                """
            )
        return [dict(row) for row in rows]
