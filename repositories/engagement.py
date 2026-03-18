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
                    jump_99k_completed_count,
                    hjd_balance,
                    hjd_lifetime_earned,
                    hjd_lifetime_spent
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 0, 0, 0)
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

    async def list_level_role_rewards(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM engagement_role_rewards
                WHERE guild_id = $1
                ORDER BY level_required ASC, id ASC
                """,
                guild_id,
            )
            return [dict(r) for r in rows]

    async def list_prize_roles(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM engagement_prize_roles
                WHERE guild_id = $1
                ORDER BY milestone_type ASC, milestone_value ASC, id ASC
                """,
                guild_id,
            )
            return [dict(r) for r in rows]

    async def set_level_reward_role_id(self, guild_id: int, level_required: int, role_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE engagement_role_rewards
                SET role_id = $3
                WHERE guild_id = $1 AND level_required = $2
                """,
                guild_id,
                level_required,
                role_id,
            )

    async def set_prize_reward_role_id(
        self, guild_id: int, milestone_type: str, milestone_value: int, role_id: int
    ) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE engagement_prize_roles
                SET role_id = $4
                WHERE guild_id = $1 AND milestone_type = $2 AND milestone_value = $3
                """,
                guild_id,
                milestone_type,
                milestone_value,
                role_id,
            )

    async def seed_default_reward_ladders(self, guild_id: int) -> None:
        level_defaults = [
            (1, "Pickpocket", "95A5A6"),
            (4, "Mugger", "7F8C8D"),
            (7, "Booster", "8E44AD"),
            (10, "Wheelman", "9B59B6"),
            (13, "Safecracker", "2980B9"),
            (16, "Drug Runner", "3498DB"),
            (19, "Racketeer", "16A085"),
            (22, "Loan Shark", "1ABC9C"),
            (25, "Bookie", "27AE60"),
            (28, "Arms Dealer", "2ECC71"),
            (31, "Heist Planner", "F39C12"),
            (34, "Faction Soldier", "F1C40F"),
            (37, "Street Boss", "D35400"),
            (40, "Underboss", "E67E22"),
            (43, "Crime Lord", "C0392B"),
            (46, "Faction Heavy", "E74C3C"),
            (49, "Torn Legend", "FF4D6D"),
            (50, "City Kingpin", "FD79A8"),
        ]
        milestone_defaults = [
            ("lifetime_tokens_earned", 5, "Supporter", "00CEC9"),
            ("lifetime_tokens_earned", 15, "High Roller", "00B894"),
            ("lifetime_tokens_earned", 30, "Whale", "55EFC4"),
            ("jump_completions", 3, "Jump Starter", "74B9FF"),
            ("jump_completions", 10, "Jump Specialist", "0984E3"),
            ("jump_completions", 25, "Airborne Addict", "6C5CE7"),
            ("raffle_purchases", 5, "Ticket Buyer", "FFEAA7"),
            ("raffle_purchases", 15, "Raffle Addict", "FDCB6E"),
            ("raffle_purchases", 30, "Jackpot Chaser", "E17055"),
            ("message_xp_total", 2500, "Talkative", "81ECEC"),
            ("message_xp_total", 10000, "Loudmouth", "00CEC9"),
            ("voice_xp_total", 1000, "Night Owl", "A29BFE"),
            ("voice_xp_total", 5000, "Radio Active", "6C5CE7"),
            ("reaction_xp_total", 500, "Crowd Favorite", "FAB1A0"),
            ("reaction_xp_total", 2000, "Local Celebrity", "FF7675"),
        ]
        async with self.acquire() as conn:
            async with conn.transaction():
                old_levels = await conn.fetch(
                    "SELECT level_required, role_id FROM engagement_role_rewards WHERE guild_id = $1",
                    guild_id,
                )
                old_milestones = await conn.fetch(
                    "SELECT milestone_type, milestone_value, role_id FROM engagement_prize_roles WHERE guild_id = $1",
                    guild_id,
                )
                level_role_ids = {int(r["level_required"]): int(r["role_id"] or 0) for r in old_levels}
                milestone_role_ids = {
                    (str(r["milestone_type"]), int(r["milestone_value"])): int(r["role_id"] or 0)
                    for r in old_milestones
                }
                await conn.execute("DELETE FROM engagement_role_rewards WHERE guild_id = $1", guild_id)
                await conn.execute("DELETE FROM engagement_prize_roles WHERE guild_id = $1", guild_id)
                for level, role_name, role_color in level_defaults:
                    await conn.execute(
                        """
                        INSERT INTO engagement_role_rewards
                        (guild_id, level_required, role_id, remove_lower_tiers, role_name, role_color, auto_created)
                        VALUES ($1, $2, $3, TRUE, $4, $5, TRUE)
                        """,
                        guild_id,
                        level,
                        int(level_role_ids.get(level, 0)),
                        role_name,
                        role_color,
                    )
                for milestone_type, milestone_value, role_name, role_color in milestone_defaults:
                    await conn.execute(
                        """
                        INSERT INTO engagement_prize_roles
                        (guild_id, milestone_type, milestone_value, role_id, role_name, role_color, auto_created)
                        VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                        """,
                        guild_id,
                        milestone_type,
                        milestone_value,
                        int(milestone_role_ids.get((milestone_type, milestone_value), 0)),
                        role_name,
                        role_color,
                    )

    async def list_profiles_for_guild(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM engagement_profiles WHERE guild_id = $1", guild_id)
            return [dict(r) for r in rows]

    async def reverse_event_by_dedupe_key(self, guild_id: int, dedupe_key: str) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE engagement_event_ledger
                SET reversed_at = NOW(), reversal_reason = 'admin_reverse_event'
                WHERE guild_id = $1 AND dedupe_key = $2 AND reversed_at IS NULL
                RETURNING *
                """,
                guild_id,
                dedupe_key,
            )
            return dict(row) if row else None

    async def rebuild_profile_from_ledgers(self, guild_id: int, user_id: int) -> dict:
        async with self.acquire() as conn:
            async with conn.transaction():
                profile = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL THEN xp_delta ELSE 0 END), 0) AS xp_total,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name='message' THEN xp_delta ELSE 0 END), 0) AS message_xp_total,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name='reaction_received' THEN xp_delta ELSE 0 END), 0) AS reaction_xp_total,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name='voice' THEN xp_delta ELSE 0 END), 0) AS voice_xp_total,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name IN ('paid_raffle_purchase_verified','raffle_prize_token_purchase_confirmed') THEN xp_delta ELSE 0 END), 0) AS paid_raffle_xp_total,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name='jump_99k_purchase_verified' THEN xp_delta ELSE 0 END), 0) AS jump_purchase_xp_total,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name='jump_99k_completed' THEN xp_delta ELSE 0 END), 0) AS jump_completion_xp_total,
                        COALESCE(COUNT(*) FILTER (WHERE reversed_at IS NULL AND event_name='paid_raffle_purchase_verified'), 0) AS paid_raffle_purchases_count,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND event_name='paid_raffle_purchase_verified' THEN COALESCE((payload_json->>'ticket_count')::INT, 0) ELSE 0 END), 0) AS paid_raffle_tickets_count,
                        COALESCE(COUNT(*) FILTER (WHERE reversed_at IS NULL AND event_name='jump_99k_purchase_verified'), 0) AS jump_99k_purchases_count,
                        COALESCE(COUNT(*) FILTER (WHERE reversed_at IS NULL AND event_name='jump_99k_completed'), 0) AS jump_99k_completed_count
                    FROM engagement_event_ledger
                    WHERE guild_id = $1 AND user_id = $2
                    """,
                    guild_id,
                    user_id,
                )
                tokens = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL THEN amount ELSE 0 END), 0) AS prize_token_balance,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND amount > 0 THEN amount ELSE 0 END), 0) AS prize_token_lifetime_earned,
                        COALESCE(SUM(CASE WHEN reversed_at IS NULL AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS prize_token_lifetime_spent
                    FROM prize_token_transactions
                    WHERE guild_id = $1 AND user_id = $2
                    """,
                    guild_id,
                    user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO engagement_profiles (guild_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, user_id) DO NOTHING
                    """,
                    guild_id,
                    user_id,
                )
                row = await conn.fetchrow(
                    """
                    UPDATE engagement_profiles
                    SET xp_total=$3, message_xp_total=$4, reaction_xp_total=$5, voice_xp_total=$6,
                        paid_raffle_xp_total=$7, jump_purchase_xp_total=$8, jump_completion_xp_total=$9,
                        paid_raffle_purchases_count=$10, paid_raffle_tickets_count=$11,
                        jump_99k_purchases_count=$12, jump_99k_completed_count=$13,
                        prize_token_balance=$14, prize_token_lifetime_earned=$15, prize_token_lifetime_spent=$16,
                        hjd_balance=$17, hjd_lifetime_earned=$18, hjd_lifetime_spent=$19,
                        updated_at=NOW()
                    WHERE guild_id=$1 AND user_id=$2
                    RETURNING *
                    """,
                    guild_id,
                    user_id,
                    int(profile["xp_total"]),
                    int(profile["message_xp_total"]),
                    int(profile["reaction_xp_total"]),
                    int(profile["voice_xp_total"]),
                    int(profile["paid_raffle_xp_total"]),
                    int(profile["jump_purchase_xp_total"]),
                    int(profile["jump_completion_xp_total"]),
                    int(profile["paid_raffle_purchases_count"]),
                    int(profile["paid_raffle_tickets_count"]),
                    int(profile["jump_99k_purchases_count"]),
                    int(profile["jump_99k_completed_count"]),
                    int(tokens["prize_token_balance"]),
                    int(tokens["prize_token_lifetime_earned"]),
                    int(tokens["prize_token_lifetime_spent"]),
                    int((await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM happy_jump_dollar_transactions WHERE guild_id = $1 AND user_id = $2 AND reversed_at IS NULL", guild_id, user_id)) or 0),
                    int((await conn.fetchval("SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) FROM happy_jump_dollar_transactions WHERE guild_id = $1 AND user_id = $2 AND reversed_at IS NULL", guild_id, user_id)) or 0),
                    int((await conn.fetchval("SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 0) FROM happy_jump_dollar_transactions WHERE guild_id = $1 AND user_id = $2 AND reversed_at IS NULL", guild_id, user_id)) or 0),
                )
                return dict(row)


    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        async with self.acquire() as conn:
            message_state = await conn.execute(
                "DELETE FROM engagement_message_state WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            reaction_state = await conn.execute(
                "DELETE FROM engagement_reaction_state WHERE guild_id = $1 AND (reactor_user_id = $2 OR target_user_id = $2)",
                guild_id,
                user_id,
            )
            profile = await conn.execute(
                "DELETE FROM engagement_profiles WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            return {
                "engagement_message_state": int(str(message_state).split()[-1]),
                "engagement_reaction_state": int(str(reaction_state).split()[-1]),
                "engagement_profiles": int(str(profile).split()[-1]),
            }

    async def list_guild_user_ids(self, guild_id: int) -> set[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id AS uid FROM engagement_profiles WHERE guild_id = $1
                UNION
                SELECT user_id AS uid FROM engagement_message_state WHERE guild_id = $1
                UNION
                SELECT reactor_user_id AS uid FROM engagement_reaction_state WHERE guild_id = $1
                UNION
                SELECT target_user_id AS uid FROM engagement_reaction_state WHERE guild_id = $1
                """,
                guild_id,
            )
            return {int(r["uid"]) for r in rows if int(r["uid"] or 0) > 0}
