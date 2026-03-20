from __future__ import annotations

from datetime import datetime, timezone

import asyncpg

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
        button_join_enabled: bool = True,
        auto_entry_enabled: bool = False,
        weighted_enabled: bool = False,
        auto_entry_max_per_user: int = 1,
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
                    button_join_enabled,
                    auto_entry_enabled,
                    weighted_odds_enabled,
                    auto_entry_max_per_user,
                    status,
                    ends_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active', $10)
                RETURNING *
                """,
                guild_id,
                channel_id,
                host_discord_id,
                prize_text,
                note_text,
                bool(button_join_enabled),
                bool(auto_entry_enabled),
                bool(weighted_enabled),
                max(1, int(auto_entry_max_per_user)),
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

    async def set_status(
        self, raffle_id: int, status: str, ended_at: datetime | None = None
    ) -> None:
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

    async def add_entry_with_source(
        self,
        raffle_id: int,
        discord_id: int,
        *,
        entry_source: str,
        entry_weight: int,
        dedupe_key: str | None,
    ) -> bool:
        async with self.acquire() as conn:
            return await self._upsert_entry_with_conn(
                conn,
                raffle_id=raffle_id,
                discord_id=discord_id,
                entry_source=entry_source,
                entry_weight=entry_weight,
                dedupe_key=dedupe_key,
                accumulate=False,
            )

    async def _upsert_entry_with_conn(
        self,
        conn: asyncpg.Connection,
        *,
        raffle_id: int,
        discord_id: int,
        entry_source: str,
        entry_weight: int,
        dedupe_key: str | None,
        accumulate: bool,
    ) -> bool:
        if accumulate:
            result = await conn.execute(
                """
                INSERT INTO free_raffle_entries (raffle_id, discord_id, entry_source, entry_weight, dedupe_key)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (raffle_id, discord_id) DO UPDATE
                SET entry_weight = free_raffle_entries.entry_weight + EXCLUDED.entry_weight,
                    entry_source = EXCLUDED.entry_source,
                    dedupe_key = EXCLUDED.dedupe_key,
                    created_at = free_raffle_entries.created_at
                WHERE free_raffle_entries.dedupe_key IS DISTINCT FROM EXCLUDED.dedupe_key
                """,
                raffle_id,
                discord_id,
                entry_source,
                max(1, int(entry_weight)),
                dedupe_key,
            )
            return not result.endswith("0")
        result = await conn.execute(
            """
            INSERT INTO free_raffle_entries (raffle_id, discord_id, entry_source, entry_weight, dedupe_key)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (raffle_id, discord_id) DO NOTHING
            """,
            raffle_id,
            discord_id,
            entry_source,
            max(1, int(entry_weight)),
            dedupe_key,
        )
        return result.endswith("1")

    async def increment_auto_entry_progress(
        self,
        *,
        guild_id: int,
        raffle_id: int,
        user_id: int,
        entry_weight: int,
        qualifying_messages: int = 1,
        progress_dedupe_key: str,
    ) -> dict:
        async with self.acquire() as conn:
            async with conn.transaction():
                raffle = await conn.fetchrow(
                    """
                    SELECT *
                    FROM free_raffles
                    WHERE id = $1
                      AND guild_id = $2
                      AND status = 'active'
                      AND COALESCE(auto_entry_enabled, FALSE) = TRUE
                    FOR UPDATE
                    """,
                    raffle_id,
                    guild_id,
                )
                if raffle is None:
                    return {"awarded": False, "entries_granted": 0, "auto_entries_granted": 0, "qualifying_message_count": 0}

                await conn.execute(
                    """
                    INSERT INTO engagement_profiles (guild_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, user_id) DO NOTHING
                    """,
                    guild_id,
                    user_id,
                )
                profile = await conn.fetchrow(
                    """
                    SELECT prize_token_balance
                    FROM engagement_profiles
                    WHERE guild_id = $1 AND user_id = $2
                    FOR UPDATE
                    """,
                    guild_id,
                    user_id,
                )
                if int(profile.get("prize_token_balance") or 0) < 1:
                    return {"awarded": False, "entries_granted": 0, "auto_entries_granted": 0, "qualifying_message_count": 0}

                progress = await conn.fetchrow(
                    """
                    INSERT INTO giveaway_auto_progress (raffle_id, guild_id, user_id, qualifying_message_count, auto_entries_granted, last_award_dedupe_key)
                    VALUES ($1, $2, $3, $4, 0, NULL)
                    ON CONFLICT (raffle_id, user_id) DO UPDATE
                    SET qualifying_message_count = giveaway_auto_progress.qualifying_message_count + EXCLUDED.qualifying_message_count,
                        updated_at = NOW()
                    RETURNING *
                    """,
                    raffle_id, guild_id, user_id, max(0, int(qualifying_messages))
                )
                progress = dict(progress)
                max_entries = max(1, int(raffle.get('auto_entry_max_per_user') or 1))
                granted = int(progress.get('auto_entries_granted') or 0)
                banked = int(progress.get('qualifying_message_count') or 0)
                entries_to_award = min(max_entries - granted, banked // 15)
                if entries_to_award <= 0:
                    return {"awarded": False, "entries_granted": 0, "auto_entries_granted": granted, "qualifying_message_count": banked}

                consumed = entries_to_award * 15
                new_granted = granted + entries_to_award
                new_banked = banked - consumed
                dedupe_key = progress_dedupe_key if entries_to_award == 1 else f"{progress_dedupe_key}:x{entries_to_award}"
                applied = await self._upsert_entry_with_conn(
                    conn,
                    raffle_id=raffle_id,
                    discord_id=user_id,
                    entry_source='auto_messages',
                    entry_weight=max(1, int(entry_weight)) * entries_to_award,
                    dedupe_key=dedupe_key,
                    accumulate=True,
                )
                if not applied:
                    return {"awarded": False, "entries_granted": 0, "auto_entries_granted": granted, "qualifying_message_count": banked}

                await conn.execute(
                    """
                    UPDATE giveaway_auto_progress
                    SET qualifying_message_count = $4,
                        auto_entries_granted = $5,
                        last_award_dedupe_key = $6,
                        updated_at = NOW()
                    WHERE raffle_id = $1 AND guild_id = $2 AND user_id = $3
                    """,
                    raffle_id, guild_id, user_id, new_banked, new_granted, dedupe_key
                )
                return {"awarded": True, "entries_granted": entries_to_award, "auto_entries_granted": new_granted, "qualifying_message_count": new_banked}


    async def get_entry(self, raffle_id: int, user_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT discord_id, entry_source, entry_weight, created_at
                FROM free_raffle_entries
                WHERE raffle_id = $1 AND discord_id = $2
                """,
                raffle_id,
                user_id,
            )
            return dict(row) if row else None

    async def get_auto_entry_progress(self, raffle_id: int, user_id: int) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT qualifying_message_count, auto_entries_granted, updated_at
                FROM giveaway_auto_progress
                WHERE raffle_id = $1 AND user_id = $2
                """,
                raffle_id,
                user_id,
            )
            if row is None:
                return {"qualifying_message_count": 0, "auto_entries_granted": 0, "updated_at": None}
            return dict(row)

    async def user_has_entry(self, raffle_id: int, discord_id: int) -> bool:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                "SELECT 1 FROM free_raffle_entries WHERE raffle_id = $1 AND discord_id = $2",
                raffle_id,
                discord_id,
            )
            return value is not None

    async def list_active_auto_entry_raffles(self, guild_id: int | None = None) -> list[dict]:
        async with self.acquire() as conn:
            if guild_id is None:
                rows = await conn.fetch(
                    "SELECT * FROM free_raffles WHERE status = 'active' AND COALESCE(auto_entry_enabled, FALSE) = TRUE"
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM free_raffles
                    WHERE status = 'active'
                      AND guild_id = $1
                      AND COALESCE(auto_entry_enabled, FALSE) = TRUE
                    """,
                    guild_id,
                )
            return [dict(r) for r in rows]

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

    async def list_entries(self, raffle_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT discord_id, entry_source, entry_weight, created_at
                FROM free_raffle_entries
                WHERE raffle_id = $1
                ORDER BY COALESCE(created_at, NOW()) ASC, discord_id ASC
                """,
                raffle_id,
            )
            return [dict(row) for row in rows]

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

    async def draw_raffle_now(self, raffle_id: int, *, now: datetime | None = None) -> dict | None:
        draw_time = now or datetime.now(timezone.utc)
        async with self.acquire() as conn:
            async with conn.transaction():
                raffle_row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM free_raffles
                    WHERE id = $1
                      AND status = 'active'
                    FOR UPDATE
                    """,
                    raffle_id,
                )
                if raffle_row is None:
                    return None

                raffle_data = dict(raffle_row)
                weighted_mode = bool(
                    raffle_data.get("weighted_odds_enabled", raffle_data.get("weighted_enabled", False))
                )
                entry_rows = await conn.fetch(
                    "SELECT discord_id, entry_weight FROM free_raffle_entries WHERE raffle_id = $1",
                    raffle_id,
                )
                entrants = [
                    (int(row["discord_id"]), max(1, int(row.get("entry_weight") or 1)))
                    for row in entry_rows
                ]
                winner_id: int | None = None
                if entrants:
                    import secrets

                    if weighted_mode:
                        total = sum(weight for _, weight in entrants)
                        pick = secrets.randbelow(total) + 1
                        running = 0
                        for entrant_id, weight in entrants:
                            running += weight
                            if running >= pick:
                                winner_id = entrant_id
                                break
                    else:
                        entrant_ids = [entrant_id for entrant_id, _weight in entrants]
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
                    "entries_count": len(entrants),
                }

    async def reroll_winner(self, raffle_id: int) -> int | None:
        async with self.acquire() as conn:
            async with conn.transaction():
                raffle_row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM free_raffles
                    WHERE id = $1
                      AND status = 'ended'
                    FOR UPDATE
                    """,
                    raffle_id,
                )
                if raffle_row is None:
                    return None
                weighted_mode = bool(raffle_row.get("weighted_odds_enabled", False))
                rows = await conn.fetch(
                    "SELECT discord_id, entry_weight FROM free_raffle_entries WHERE raffle_id = $1",
                    raffle_id,
                )
                entrants = [
                    (int(row["discord_id"]), max(1, int(row.get("entry_weight") or 1)))
                    for row in rows
                ]
                if not entrants:
                    return None
                import secrets

                if weighted_mode:
                    total = sum(weight for _, weight in entrants)
                    pick = secrets.randbelow(total) + 1
                    running = 0
                    winner_id: int | None = None
                    for entrant_id, weight in entrants:
                        running += weight
                        if running >= pick:
                            winner_id = entrant_id
                            break
                else:
                    winner_id = int(secrets.choice([entrant_id for entrant_id, _weight in entrants]))
                await conn.execute(
                    """
                    UPDATE free_raffles
                    SET winner_discord_id = $2,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    raffle_id,
                    str(winner_id),
                )
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
                return winner_id

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

    async def draw_expired_raffle(
        self, raffle_id: int, *, now: datetime | None = None
    ) -> dict | None:
        draw_time = now or datetime.now(timezone.utc)
        async with self.acquire() as conn:
            async with conn.transaction():
                can_draw = await conn.fetchrow(
                    """
                    SELECT id
                    FROM free_raffles
                    WHERE id = $1
                      AND status = 'active'
                      AND ends_at <= $2
                    """,
                    raffle_id,
                    draw_time,
                )
                if can_draw is None:
                    return None
        return await self.draw_raffle_now(raffle_id, now=draw_time)

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

    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM free_raffle_entries e
                USING free_raffles r
                WHERE e.raffle_id = r.id
                  AND r.guild_id = $1
                  AND e.discord_id = $2
                """,
                guild_id,
                user_id,
            )
            return {"free_raffle_entries": int(str(result).split()[-1])}

    async def list_guild_participant_user_ids(self, guild_id: int) -> set[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT e.discord_id
                FROM free_raffle_entries e
                JOIN free_raffles r ON r.id = e.raffle_id
                WHERE r.guild_id = $1
                """,
                guild_id,
            )
            return {int(r["discord_id"]) for r in rows if int(r["discord_id"] or 0) > 0}
