from __future__ import annotations

from typing import Any, Optional

from .base import RepositoryBase


class JumpsRepository(RepositoryBase):
    _has_scheduled_start_text: Optional[bool] = None

    async def _scheduled_start_supported(self) -> bool:
        if self.__class__._has_scheduled_start_text is not None:
            return bool(self.__class__._has_scheduled_start_text)
        async with self.pool.acquire() as conn:
            has_column = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'jump_99k_sessions'
                      AND column_name = 'scheduled_start_text'
                )
                """
            )
        self.__class__._has_scheduled_start_text = bool(has_column)
        return bool(has_column)

    def _merge_notes_with_start(self, notes: Optional[str], scheduled_start_text: Optional[str]) -> Optional[str]:
        clean_notes = (notes or "").strip()
        clean_start = (scheduled_start_text or "").strip()
        if not clean_start:
            return clean_notes or None
        start_line = f"Possible TCT start: {clean_start}"
        if not clean_notes:
            return start_line
        return f"{start_line}\n{clean_notes}"

    async def get_settings(self, guild_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_settings WHERE guild_id = $1", guild_id)
            return dict(row) if row else None

    async def upsert_settings(
        self,
        *,
        guild_id: int,
        host_role_id: int,
        announce_channel_id: Optional[int],
        insurance_role_id: Optional[int],
        insurance_channel_id: Optional[int],
        default_max_slots: int,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jump_99k_settings (
                    guild_id, host_role_id, announce_channel_id, insurance_role_id, insurance_channel_id, default_max_slots, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (guild_id)
                DO UPDATE SET
                    host_role_id = EXCLUDED.host_role_id,
                    announce_channel_id = EXCLUDED.announce_channel_id,
                    insurance_role_id = EXCLUDED.insurance_role_id,
                    insurance_channel_id = EXCLUDED.insurance_channel_id,
                    default_max_slots = EXCLUDED.default_max_slots,
                    updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                host_role_id,
                announce_channel_id,
                insurance_role_id,
                insurance_channel_id,
                default_max_slots,
            )
            return dict(row)

    async def create_session(self, *, guild_id: int, host_discord_id: int, title: str, scheduled_start_text: Optional[str], max_slots: int, notes: Optional[str], price_item: str, price_quantity: int, announce_channel_id: Optional[int], announce_message_id: Optional[int]) -> int:
        if await self._scheduled_start_supported():
            query = """
                INSERT INTO jump_99k_sessions (
                    guild_id, host_discord_id, title, scheduled_start_text, max_slots, notes, price_item, price_quantity, status, announce_channel_id, announce_message_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'open',$9,$10)
                RETURNING id
                """
            params = (
                guild_id,
                host_discord_id,
                title,
                scheduled_start_text,
                max_slots,
                notes,
                price_item,
                price_quantity,
                announce_channel_id,
                announce_message_id,
            )
        else:
            merged_notes = self._merge_notes_with_start(notes, scheduled_start_text)
            query = """
                INSERT INTO jump_99k_sessions (
                    guild_id, host_discord_id, title, max_slots, notes, price_item, price_quantity, status, announce_channel_id, announce_message_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,'open',$8,$9)
                RETURNING id
                """
            params = (
                guild_id,
                host_discord_id,
                title,
                max_slots,
                merged_notes,
                price_item,
                price_quantity,
                announce_channel_id,
                announce_message_id,
            )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return int(row["id"])

    async def update_session(self, session_id: int, *, title: str, scheduled_start_text: Optional[str], max_slots: int, notes: Optional[str], price_item: str, price_quantity: int) -> bool:
        if await self._scheduled_start_supported():
            query = """
                UPDATE jump_99k_sessions
                SET title=$2, scheduled_start_text=$3, max_slots=$4, notes=$5, price_item=$6, price_quantity=$7
                WHERE id=$1 AND status='open'
                RETURNING id
                """
            params = (session_id, title, scheduled_start_text, max_slots, notes, price_item, price_quantity)
        else:
            merged_notes = self._merge_notes_with_start(notes, scheduled_start_text)
            query = """
                UPDATE jump_99k_sessions
                SET title=$2, max_slots=$3, notes=$4, price_item=$5, price_quantity=$6
                WHERE id=$1 AND status='open'
                RETURNING id
                """
            params = (session_id, title, max_slots, merged_notes, price_item, price_quantity)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return row is not None

    async def set_announcement_message(self, session_id: int, *, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE jump_99k_sessions SET announce_channel_id = $2, announce_message_id = $3 WHERE id = $1", session_id, channel_id, message_id)

    async def get_active_session(self, guild_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_sessions WHERE guild_id = $1 AND status = 'open' ORDER BY created_at DESC LIMIT 1", guild_id)
            return dict(row) if row else None

    async def get_session(self, session_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_sessions WHERE id = $1", session_id)
            return dict(row) if row else None

    async def signup_count(self, session_id: int) -> int:
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM jump_99k_signups WHERE session_id = $1 AND status IN ('signed_up','completed','not_completed')", session_id))

    async def create_or_restore_signup(self, *, session_id: int, guild_id: int, discord_id: int, torn_user_id: Optional[int]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jump_99k_signups (session_id, guild_id, discord_id, torn_user_id, status)
                VALUES ($1,$2,$3,$4,'signed_up')
                ON CONFLICT (session_id, discord_id)
                DO UPDATE SET status = 'signed_up', torn_user_id = EXCLUDED.torn_user_id
                """,
                session_id,
                guild_id,
                discord_id,
                torn_user_id,
            )

    async def cancel_signup(self, *, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE jump_99k_signups SET status = 'cancelled' WHERE session_id = $1 AND discord_id = $2 RETURNING id", session_id, discord_id)
            return row is not None

    async def list_signups(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM jump_99k_signups WHERE session_id = $1 ORDER BY signed_up_at ASC", session_id)
            return [dict(r) for r in rows]

    async def upsert_readiness_snapshot(self, *, session_id: int, discord_id: int, energy: int, energy_max: int, drug_cooldown: int, status_text: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jump_99k_readiness (session_id, discord_id, energy, energy_max, drug_cooldown, status_text, checked_at)
                VALUES ($1,$2,$3,$4,$5,$6,NOW())
                ON CONFLICT (session_id, discord_id)
                DO UPDATE SET energy=EXCLUDED.energy, energy_max=EXCLUDED.energy_max, drug_cooldown=EXCLUDED.drug_cooldown, status_text=EXCLUDED.status_text, checked_at=NOW()
                """,
                session_id, discord_id, energy, energy_max, drug_cooldown, status_text,
            )

    async def list_signups_with_readiness(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.*, r.energy, r.energy_max, r.drug_cooldown, r.status_text, r.checked_at
                FROM jump_99k_signups s
                LEFT JOIN jump_99k_readiness r ON r.session_id=s.session_id AND r.discord_id=s.discord_id
                WHERE s.session_id=$1
                ORDER BY s.signed_up_at ASC
                """,
                session_id,
            )
            return [dict(r) for r in rows]

    async def mark_signup_payment_verified(self, *, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jump_99k_signups SET payment_verified=true, payment_verified_at=NOW() WHERE session_id=$1 AND discord_id=$2 RETURNING id",
                session_id,
                discord_id,
            )
            return row is not None

    async def mark_signup_overdose(self, *, session_id: int, discord_id: int, overdose_meta: dict[str, Any]) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jump_99k_signups SET overdose_flag=true, overdose_detected_at=NOW(), overdose_meta=$3::jsonb WHERE session_id=$1 AND discord_id=$2 RETURNING id",
                session_id,
                discord_id,
                overdose_meta,
            )
            return row is not None

    async def close_session_and_record(self, *, session_id: int, guild_id: int, completed_discord_ids: list[int], not_completed_discord_ids: list[int]) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("UPDATE jump_99k_sessions SET status = 'closed', closed_at = NOW() WHERE id = $1 AND status = 'open' RETURNING id", session_id)
                if not row:
                    return False
                if completed_discord_ids:
                    await conn.execute("UPDATE jump_99k_signups SET status = 'completed' WHERE session_id = $1 AND discord_id = ANY($2::bigint[])", session_id, completed_discord_ids)
                if not_completed_discord_ids:
                    await conn.execute("UPDATE jump_99k_signups SET status = 'not_completed' WHERE session_id = $1 AND discord_id = ANY($2::bigint[])", session_id, not_completed_discord_ids)
                await conn.execute(
                    """
                    INSERT INTO jump_99k_totals (guild_id, completed_count, not_completed_count, updated_at)
                    VALUES ($1,$2,$3,NOW())
                    ON CONFLICT (guild_id)
                    DO UPDATE SET completed_count = jump_99k_totals.completed_count + EXCLUDED.completed_count,
                                  not_completed_count = jump_99k_totals.not_completed_count + EXCLUDED.not_completed_count,
                                  updated_at = NOW()
                    """,
                    guild_id,
                    len(completed_discord_ids),
                    len(not_completed_discord_ids),
                )
                await conn.execute("DELETE FROM jump_99k_readiness WHERE session_id = $1", session_id)
                return True

    async def create_insurer_profile(self, *, guild_id: int, insurer_discord_id: int, display_name: str, policy_summary: str, contact_instructions: str, metadata: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jump_99k_insurers (guild_id, insurer_discord_id, display_name, policy_summary, contact_instructions, insurer_meta, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,NOW())
                ON CONFLICT (guild_id, insurer_discord_id)
                DO UPDATE SET display_name=EXCLUDED.display_name, policy_summary=EXCLUDED.policy_summary, contact_instructions=EXCLUDED.contact_instructions, insurer_meta=EXCLUDED.insurer_meta, updated_at=NOW()
                """,
                guild_id, insurer_discord_id, display_name, policy_summary, contact_instructions, metadata,
            )

    async def get_insurer_profile(self, *, guild_id: int, insurer_discord_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_insurers WHERE guild_id=$1 AND insurer_discord_id=$2", guild_id, insurer_discord_id)
            return dict(row) if row else None

    async def create_insurance_request(self, *, session_id: int, participant_discord_id: int, channel_id: int, message_id: int) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jump_99k_insurance_requests (session_id, participant_discord_id, status, requested_at, channel_id, message_id)
                VALUES ($1,$2,'requested',NOW(),$3,$4)
                ON CONFLICT (session_id, participant_discord_id)
                DO UPDATE SET status='requested', requested_at=NOW(), channel_id=EXCLUDED.channel_id, message_id=EXCLUDED.message_id
                RETURNING id
                """,
                session_id,
                participant_discord_id,
                channel_id,
                message_id,
            )
            return int(row["id"])

    async def get_insurance_request(self, request_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_insurance_requests WHERE id=$1", request_id)
            return dict(row) if row else None

    async def claim_insurance_request(self, *, request_id: int, claimed_by_discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jump_99k_insurance_requests
                SET status='claimed', claimed_by_discord_id=$2, claimed_at=NOW()
                WHERE id=$1 AND status='requested'
                RETURNING id
                """,
                request_id,
                claimed_by_discord_id,
            )
            return row is not None

    async def set_insurance_request_status(self, *, request_id: int, status: str, actor_discord_id: Optional[int] = None) -> bool:
        column_sql = {
            "denied": "denied_by_discord_id = $3, denied_at = NOW()",
            "accepted": "accepted_at = NOW()",
            "declined": "declined_at = NOW()",
            "completed": "completed_at = NOW()",
        }
        if status not in column_sql:
            return False
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE jump_99k_insurance_requests SET status=$2, {column_sql[status]} WHERE id=$1 RETURNING id",
                request_id,
                status,
                actor_discord_id,
            )
            return row is not None

    async def get_totals(self, guild_id: int) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_totals WHERE guild_id = $1", guild_id)
            return dict(row) if row else {"guild_id": guild_id, "completed_count": 0, "not_completed_count": 0}
