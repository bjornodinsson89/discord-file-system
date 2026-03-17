from __future__ import annotations

import json
from typing import Any

from .base import RepositoryBase


class EngagementRepository(RepositoryBase):
    async def get_or_create_guild_settings(self, guild_id: int) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO engagement_guild_settings (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id) DO UPDATE SET guild_id = EXCLUDED.guild_id
                RETURNING *
                """,
                guild_id,
            )
            return dict(row)


    async def upsert_guild_settings(self, guild_id: int, **changes: Any) -> dict:
        if not changes:
            return await self.get_or_create_guild_settings(guild_id)
        allowed = {
            "enabled",
            "levelup_channel_id",
            "leaderboard_enabled",
            "profile_cards_enabled",
            "message_xp_enabled",
            "reaction_xp_enabled",
            "voice_xp_enabled",
            "paid_raffle_purchase_xp_base",
            "paid_raffle_purchase_xp_per_ticket",
            "paid_raffle_purchase_xp_cap",
            "jump_purchase_xp",
            "jump_completion_xp",
            "auto_entry_giveaways_enabled",
            "ignored_channel_ids_json",
            "ignored_category_ids_json",
            "ignored_role_ids_json",
        }
        keys = [k for k in changes if k in allowed]
        if not keys:
            return await self.get_or_create_guild_settings(guild_id)
        await self.get_or_create_guild_settings(guild_id)
        sets = [f"{k} = ${idx + 2}" for idx, k in enumerate(keys)]
        values = [changes[k] for k in keys]
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE engagement_guild_settings SET {', '.join(sets)}, updated_at = NOW() WHERE guild_id = $1 RETURNING *",
                guild_id,
                *values,
            )
            return dict(row)

    async def get_or_create_profile(self, guild_id: int, user_id: int) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO engagement_profiles (guild_id, user_id)
                VALUES ($1, $2)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                SET updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                user_id,
            )
            return dict(row)

    async def apply_xp_delta(
        self, guild_id: int, user_id: int, xp_delta: int, increments: dict[str, int]
    ) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO engagement_profiles (
                    guild_id,
                    user_id,
                    xp_total,
                    message_xp_total,
                    reaction_xp_total,
                    voice_xp_total,
                    paid_raffle_xp_total,
                    jump_purchase_xp_total,
                    jump_completion_xp_total,
                    paid_raffle_purchases_count,
                    paid_raffle_tickets_count,
                    jump_99k_purchases_count,
                    jump_99k_completed_count
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                SET xp_total = engagement_profiles.xp_total + EXCLUDED.xp_total,
                    message_xp_total = engagement_profiles.message_xp_total + EXCLUDED.message_xp_total,
                    reaction_xp_total = engagement_profiles.reaction_xp_total + EXCLUDED.reaction_xp_total,
                    voice_xp_total = engagement_profiles.voice_xp_total + EXCLUDED.voice_xp_total,
                    paid_raffle_xp_total = engagement_profiles.paid_raffle_xp_total + EXCLUDED.paid_raffle_xp_total,
                    jump_purchase_xp_total = engagement_profiles.jump_purchase_xp_total + EXCLUDED.jump_purchase_xp_total,
                    jump_completion_xp_total = engagement_profiles.jump_completion_xp_total + EXCLUDED.jump_completion_xp_total,
                    paid_raffle_purchases_count = engagement_profiles.paid_raffle_purchases_count + EXCLUDED.paid_raffle_purchases_count,
                    paid_raffle_tickets_count = engagement_profiles.paid_raffle_tickets_count + EXCLUDED.paid_raffle_tickets_count,
                    jump_99k_purchases_count = engagement_profiles.jump_99k_purchases_count + EXCLUDED.jump_99k_purchases_count,
                    jump_99k_completed_count = engagement_profiles.jump_99k_completed_count + EXCLUDED.jump_99k_completed_count,
                    updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                user_id,
                xp_delta,
                int(increments.get("message_xp_total", 0)),
                int(increments.get("reaction_xp_total", 0)),
                int(increments.get("voice_xp_total", 0)),
                int(increments.get("paid_raffle_xp_total", 0)),
                int(increments.get("jump_purchase_xp_total", 0)),
                int(increments.get("jump_completion_xp_total", 0)),
                int(increments.get("paid_raffle_purchases_count", 0)),
                int(increments.get("paid_raffle_tickets_count", 0)),
                int(increments.get("jump_99k_purchases_count", 0)),
                int(increments.get("jump_99k_completed_count", 0)),
            )
            return dict(row)

    async def get_leaderboard(self, guild_id: int, board: str, limit: int = 10) -> list[dict]:
        order_map = {
            "xp": "xp_total DESC, user_id ASC",
            "levels": "level DESC, xp_total DESC, user_id ASC",
            "tokens": "prize_token_lifetime_earned DESC, user_id ASC",
            "jumps": "jump_99k_completed_count DESC, user_id ASC",
            "raffles": "paid_raffle_tickets_count DESC, user_id ASC",
        }
        order_by = order_map.get(board)
        if not order_by:
            raise ValueError(f"unsupported leaderboard type: {board}")
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM engagement_profiles WHERE guild_id=$1 ORDER BY {order_by} LIMIT $2",
                guild_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def update_level(self, guild_id: int, user_id: int, level: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE engagement_profiles SET level = $3, updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
                level,
            )

    async def insert_event_ledger(
        self,
        *,
        guild_id: int,
        user_id: int,
        event_name: str,
        source_type: str,
        source_id: str,
        dedupe_key: str,
        xp_delta: int,
        payload: dict[str, Any] | None,
    ) -> bool:
        payload_json = json.dumps(payload or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO engagement_event_ledger (
                    guild_id, user_id, event_name, source_type, source_id, dedupe_key, xp_delta, payload_json
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                ON CONFLICT (guild_id, dedupe_key) DO NOTHING
                RETURNING id
                """,
                guild_id,
                user_id,
                event_name,
                source_type,
                source_id,
                dedupe_key,
                xp_delta,
                payload_json,
            )
            return row is not None

    async def get_message_state(self, guild_id: int, user_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM engagement_message_state WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            return dict(row) if row else None

    async def upsert_message_state(
        self,
        guild_id: int,
        user_id: int,
        *,
        last_eligible_message_at,
        last_message_fingerprint: str,
        last_channel_id: int,
    ) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO engagement_message_state (guild_id, user_id, last_eligible_message_at, last_message_fingerprint, last_channel_id)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                SET last_eligible_message_at = EXCLUDED.last_eligible_message_at,
                    last_message_fingerprint = EXCLUDED.last_message_fingerprint,
                    last_channel_id = EXCLUDED.last_channel_id,
                    updated_at = NOW()
                """,
                guild_id,
                user_id,
                last_eligible_message_at,
                last_message_fingerprint,
                last_channel_id,
            )

    async def add_reaction_marker(
        self, guild_id: int, message_id: int, reactor_user_id: int, target_user_id: int
    ) -> bool:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO engagement_reaction_state (guild_id, message_id, reactor_user_id, target_user_id)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (guild_id, message_id, reactor_user_id) DO NOTHING
                RETURNING guild_id
                """,
                guild_id,
                message_id,
                reactor_user_id,
                target_user_id,
            )
            return row is not None

    async def get_recent_event_rows(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM engagement_event_ledger
                WHERE guild_id = $1 AND user_id = $2
                ORDER BY id DESC LIMIT $3
                """,
                guild_id,
                user_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_recent_profiles_by_xp(self, guild_id: int, limit: int = 20) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM engagement_profiles WHERE guild_id=$1 ORDER BY xp_total DESC, user_id ASC LIMIT $2",
                guild_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def get_rank(self, guild_id: int, user_id: int) -> int:
        async with self.acquire() as conn:
            rank = await conn.fetchval(
                """
                SELECT COALESCE((
                    SELECT 1 + COUNT(*) FROM engagement_profiles p
                    WHERE p.guild_id=$1
                    AND (p.xp_total > me.xp_total OR (p.xp_total = me.xp_total AND p.user_id < me.user_id))
                ),1)
                FROM engagement_profiles me
                WHERE me.guild_id=$1 AND me.user_id=$2
                """,
                guild_id,
                user_id,
            )
            return int(rank or 1)

    async def get_hourly_reaction_xp(self, guild_id: int, user_id: int) -> int:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(SUM(xp_delta), 0)
                FROM engagement_event_ledger
                WHERE guild_id = $1
                  AND user_id = $2
                  AND event_name = 'reaction_received'
                  AND created_at >= (NOW() - INTERVAL '1 hour')
                """,
                guild_id,
                user_id,
            )
            return int(value or 0)
