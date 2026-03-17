from __future__ import annotations

from .base import RepositoryBase


class PoolsRepository(RepositoryBase):
    async def create_pool(
        self,
        guild_id: int,
        created_by_discord_id: int,
        ticket_price_xanax: int,
        tickets_total: int | None,
        max_per_user: int,
        announce_channel_id: int | None,
        panel_channel_id: int | None,
        unlimited_tickets: bool = False,
        end_draw_at=None,
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
                    panel_channel_id,
                    unlimited_tickets,
                    end_draw_at
                ) VALUES ($1, $2, 'active', $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                guild_id,
                created_by_discord_id,
                ticket_price_xanax,
                tickets_total,
                max_per_user,
                announce_channel_id,
                panel_channel_id,
                unlimited_tickets,
                end_draw_at,
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
                       panel_channel_id, panel_message_id, created_at, unlimited_tickets, end_draw_at
                FROM xanax_pools
                WHERE guild_id = $1 AND status = 'active'
                ORDER BY created_at DESC
                """,
                guild_id,
            )
        return [dict(row) for row in rows]

    async def list_due_pools(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM xanax_pools
                WHERE status = 'active'
                  AND end_draw_at IS NOT NULL
                  AND end_draw_at <= NOW()
                ORDER BY end_draw_at ASC, id ASC
                """
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

    async def create_or_replace_pending_purchase(
        self,
        pool_id: int,
        guild_id: int,
        buyer_discord_id: int,
        buyer_torn_user_id: int,
        buyer_torn_name: str | None,
        identity_source: str,
        quantity: int,
        total_cost_xanax: int,
        reserved_until,
    ) -> dict:
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE public.xanax_pool_pending_purchases
                    SET verified_at = NOW(),
                        updated_at = NOW()
                    WHERE pool_id = $1
                      AND buyer_discord_id = $2
                      AND verified_at IS NULL
                    """,
                    int(pool_id),
                    int(buyer_discord_id),
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO public.xanax_pool_pending_purchases (
                        pool_id,
                        guild_id,
                        buyer_discord_id,
                        buyer_torn_user_id,
                        buyer_torn_name,
                        identity_source,
                        quantity,
                        total_cost_xanax,
                        reserved_until,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), NOW())
                    RETURNING *
                    """,
                    int(pool_id),
                    int(guild_id),
                    int(buyer_discord_id),
                    int(buyer_torn_user_id),
                    buyer_torn_name,
                    identity_source,
                    int(quantity),
                    int(total_cost_xanax),
                    reserved_until,
                )
        return dict(row)

    async def get_pending_purchase(self, pool_id: int, buyer_discord_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM public.xanax_pool_pending_purchases
                WHERE pool_id = $1
                  AND buyer_discord_id = $2
                  AND verified_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                int(pool_id),
                int(buyer_discord_id),
            )
        return dict(row) if row else None

    async def mark_pending_purchase_verified(self, pending_id: int) -> bool:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE public.xanax_pool_pending_purchases
                SET verified_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                  AND verified_at IS NULL
                """,
                int(pending_id),
            )
        return result == "UPDATE 1"

    async def delete_expired_pending_purchases(self) -> int:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM public.xanax_pool_pending_purchases
                WHERE verified_at IS NULL
                  AND reserved_until < NOW()
                """
            )
        try:
            return int(str(result).split()[-1])
        except Exception:
            return 0


    async def delete_pending_purchase(self, pool_id: int, buyer_discord_id: int) -> bool:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM public.xanax_pool_pending_purchases
                WHERE pool_id = $1
                  AND buyer_discord_id = $2
                  AND verified_at IS NULL
                """,
                int(pool_id),
                int(buyer_discord_id),
            )
        return result.startswith("DELETE ") and int(result.split()[-1]) > 0

    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        async with self.acquire() as conn:
            entry_result = await conn.execute(
                """
                DELETE FROM xanax_pool_entries e
                USING xanax_pools p
                WHERE e.pool_id = p.id
                  AND p.guild_id = $1
                  AND e.user_discord_id = $2
                """,
                guild_id,
                user_id,
            )
            pending_result = await conn.execute(
                "DELETE FROM xanax_pool_pending_purchases WHERE guild_id = $1 AND buyer_discord_id = $2",
                guild_id,
                user_id,
            )
            return {
                "xanax_pool_entries": int(str(entry_result).split()[-1]),
                "xanax_pool_pending_purchases": int(str(pending_result).split()[-1]),
            }

    async def list_guild_participant_user_ids(self, guild_id: int) -> set[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT e.user_discord_id AS uid
                FROM xanax_pool_entries e
                JOIN xanax_pools p ON p.id = e.pool_id
                WHERE p.guild_id = $1
                UNION
                SELECT DISTINCT buyer_discord_id AS uid
                FROM xanax_pool_pending_purchases
                WHERE guild_id = $1
                """,
                guild_id,
            )
            return {int(r["uid"]) for r in rows if int(r["uid"] or 0) > 0}
