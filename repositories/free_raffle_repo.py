from __future__ import annotations

from datetime import datetime, timezone

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
        ends_at: datetime,
    ) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO free_raffles (
                    guild_id,
                    channel_id,
                    host_discord_id,
                    prize_text,
                    note_text,
                    status,
                    ends_at
                )
                VALUES ($1, $2, $3, $4, $5, 'active', $6)
                RETURNING *
                """,
                guild_id,
                channel_id,
                host_discord_id,
                prize_text,
                note_text,
                ends_at,
            )
            return dict(row)

    async def set_message_id(self, raffle_id: int, message_id: int) -> None:
        async with self.acquire() as conn:
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
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM free_raffles WHERE id = $1",
                raffle_id,
            )
            return dict(row) if row else None

    async def set_status(self, raffle_id: int, status: str, ended_at: datetime | None = None) -> None:
        async with self.acquire() as conn:
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
        async with self.acquire() as conn:
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
        async with self.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM free_raffle_entries WHERE raffle_id = $1",
                raffle_id,
            )
            return int(value or 0)

    async def list_entry_ids(self, raffle_id: int) -> list[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT discord_id FROM free_raffle_entries WHERE raffle_id = $1",
                raffle_id,
            )
            return [int(row["discord_id"]) for row in rows]

    async def create_winner(self, raffle_id: int, discord_id: int) -> None:
        async with self.acquire() as conn:
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
        async with self.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(
                    winner_discord_id,
                    (SELECT discord_id::TEXT FROM free_raffle_winners WHERE raffle_id = $1)
                )
                FROM free_raffles
                WHERE id = $1
                """,
                raffle_id,
            )
            return int(value) if value is not None else None

    async def list_active_raffles(self, guild_id: int | None = None) -> list[dict]:
        async with self.acquire() as conn:
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

    async def list_expired_active_raffles(self, *, now: datetime, limit: int = 10) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM free_raffles
                WHERE status = 'active'
                  AND ends_at <= $1
                ORDER BY ends_at ASC
                LIMIT $2
                """,
                now,
                limit,
            )
            return [dict(row) for row in rows]

    async def draw_expired_raffle(self, raffle_id: int, *, now: datetime | None = None) -> dict | None:
        draw_time = now or datetime.now(timezone.utc)
        async with self.acquire() as conn:
            async with conn.transaction():
                raffle_row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM free_raffles
                    WHERE id = $1
                      AND status = 'active'
                      AND ends_at <= $2
                    FOR UPDATE
                    """,
                    raffle_id,
                    draw_time,
                )
                if raffle_row is None:
                    return None

                entry_rows = await conn.fetch(
                    "SELECT discord_id FROM free_raffle_entries WHERE raffle_id = $1",
                    raffle_id,
                )
                entrant_ids = [int(row["discord_id"]) for row in entry_rows]
                winner_id: int | None = None
                if entrant_ids:
                    import secrets

                    winner_id = int(secrets.choice(entrant_ids))

                updated_row = await conn.fetchrow(
                    """
                    UPDATE free_raffles
                    SET status = 'ended',
                        winner_discord_id = $3,
                        drawn_at = $2,
                        ended_at = COALESCE(ended_at, $2),
                        updated_at = NOW()
                    WHERE id = $1
                      AND status = 'active'
                    RETURNING *
                    """,
                    raffle_id,
                    draw_time,
                    str(winner_id) if winner_id is not None else None,
                )
                if updated_row is None:
                    return None

                if winner_id is not None:
                    await conn.execute(
                        """
                        INSERT INTO free_raffle_winners (raffle_id, discord_id)
                        VALUES ($1, $2)
                        ON CONFLICT (raffle_id) DO UPDATE
                        SET discord_id = EXCLUDED.discord_id,
                            created_at = NOW()
                        """,
                        raffle_id,
                        winner_id,
                    )

                return {
                    "raffle": dict(updated_row),
                    "winner_id": winner_id,
                    "entries_count": len(entrant_ids),
                }

    async def backfill_missing_ends_at(self) -> int:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE free_raffles
                SET ends_at = created_at + INTERVAL '1 day',
                    updated_at = NOW()
                WHERE status = 'active'
                  AND ends_at IS NULL
                """
            )
            return int(result.split()[-1])
