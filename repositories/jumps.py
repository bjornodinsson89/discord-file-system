from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import asyncpg

from .base import RepositoryBase


log = logging.getLogger("happy_jumper.jumps_repo")

_RESERVED_UNTIL_MIGRATION_HINT = (
    "Missing DB column jump_99k_signups.reserved_until. "
    "Run migration SQL: "
    "ALTER TABLE public.jump_99k_signups ADD COLUMN IF NOT EXISTS reserved_until timestamptz; "
    "CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_reserved_until ON public.jump_99k_signups (reserved_until);"
)

_OVERDOSE_MIGRATION_HINT = (
    "Missing OD schema on jump_99k_signups (overdose_flag/overdose_detected_at/overdose_meta). "
    "Run the manual SQL migration before enabling overdose tracking."
)

_READINESS_MIGRATION_HINT = (
    "Missing jump_99k_readiness.booster_cooldown column. Run manual SQL migration before readiness polling."
)

_JUMP_PROGRESS_MIGRATION_HINT = (
    "Missing 99k jump progress schema columns (host_jump_state/jump_state). "
    "Run migration 2026_02_18_add_99k_jump_progress.sql."
)

_ALLOWED_SIGNUP_STATUSES = {"reserved", "signed_up"}


def _raise_reserved_until_migration_error(exc: Exception) -> None:
    if isinstance(exc, asyncpg.UndefinedColumnError) and "reserved_until" in str(exc):
        log.error(_RESERVED_UNTIL_MIGRATION_HINT)
    raise exc


class JumpsRepository(RepositoryBase):
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
        if default_max_slots < 1 or default_max_slots > 7:
            raise ValueError("default_max_slots must be between 1 and 7.")
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

    async def create_session(
        self,
        *,
        guild_id: int,
        host_discord_id: int,
        title: str,
        scheduled_start_text: Optional[str],
        start_time: Optional[datetime],
        max_slots: int,
        notes: Optional[str],
        price_item: str,
        price_amount: int,
        announce_channel_id: Optional[int],
        announce_message_id: Optional[int],
    ) -> int:
        if price_amount is None or int(price_amount) < 1:
            raise ValueError("price_amount must be a positive integer (>= 1).")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jump_99k_sessions (
                    guild_id, host_discord_id, title, scheduled_start_text, start_time, max_slots, notes, price_item, price_amount, status, announce_channel_id, announce_message_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'open',$10,$11)
                RETURNING id
                """,
                guild_id,
                host_discord_id,
                title,
                scheduled_start_text,
                start_time,
                max_slots,
                notes,
                price_item,
                price_amount,
                announce_channel_id,
                announce_message_id,
            )
            return int(row["id"])

    async def update_session(
        self,
        session_id: int,
        *,
        title: str,
        scheduled_start_text: Optional[str],
        start_time: Optional[datetime],
        max_slots: int,
        notes: Optional[str],
        price_item: str,
        price_amount: int,
    ) -> bool:
        if price_amount is None or int(price_amount) < 1:
            raise ValueError("price_amount must be a positive integer (>= 1).")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jump_99k_sessions
                SET title=$2, scheduled_start_text=$3, start_time=$4, max_slots=$5, notes=$6, price_item=$7, price_amount=$8
                WHERE id=$1 AND status='open'
                RETURNING id
                """,
                session_id,
                title,
                scheduled_start_text,
                start_time,
                max_slots,
                notes,
                price_item,
                price_amount,
            )
            return row is not None

    async def set_announcement_message(self, session_id: int, *, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE jump_99k_sessions SET announce_channel_id = $2, announce_message_id = $3 WHERE id = $1", session_id, channel_id, message_id)

    async def set_private_channel(self, session_id: int, *, channel_id: int, roster_message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET private_channel_id = $2,
                    roster_channel_id = $2,
                    roster_message_id = $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
                channel_id,
                roster_message_id,
            )

    async def set_roster_panel_message(self, session_id: int, *, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET roster_channel_id = $2,
                    roster_message_id = $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
                channel_id,
                message_id,
            )

    async def clear_roster_panel_message(self, session_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET roster_channel_id = NULL,
                    roster_message_id = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
            )

    async def touch_roster_refreshed(self, session_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET roster_last_refreshed_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
            )

    async def clear_private_channel(self, session_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET private_channel_id = NULL,
                    roster_channel_id = NULL,
                    roster_message_id = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
            )

    async def get_active_session(self, guild_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_sessions WHERE guild_id = $1 AND status = 'open' ORDER BY created_at DESC LIMIT 1", guild_id)
            return dict(row) if row else None

    async def list_open_sessions(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jump_99k_sessions WHERE status='open' ORDER BY created_at DESC"
            )
            return [dict(row) for row in rows]

    async def list_open_sessions_for_guild(self, guild_id: int) -> list[dict]:
        """Return all open 99k sessions for a specific guild."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jump_99k_sessions WHERE guild_id = $1 AND status='open' ORDER BY created_at DESC",
                guild_id,
            )
            return [dict(row) for row in rows]

    async def get_session(self, session_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_sessions WHERE id = $1", session_id)
            return dict(row) if row else None


    async def get_legacy_happy_jump_session(self, jump_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM happy_jump_sessions WHERE id = $1", jump_id)
            return dict(row) if row else None

    async def list_legacy_happy_jump_signups(self, jump_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT discord_id, torn_user_id, payment_verified, payment_verified_at FROM happy_jump_signups WHERE session_id = $1",
                jump_id,
            )
            return [dict(row) for row in rows]
    async def signup_count(self, session_id: int) -> int:
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM jump_99k_signups WHERE session_id = $1 AND status IN ('signed_up','completed','not_completed')", session_id))

    async def _create_or_restore_signup_on_conn(
        self,
        conn: asyncpg.Connection,
        *,
        session_id: int,
        guild_id: int,
        discord_id: int,
        torn_user_id: Optional[int],
        status: str,
        reserved_until: Optional[datetime] = None,
    ) -> None:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in _ALLOWED_SIGNUP_STATUSES:
            raise ValueError(f"status must be one of {_ALLOWED_SIGNUP_STATUSES}.")
        try:
            await conn.execute(
                """
                INSERT INTO jump_99k_signups (session_id, guild_id, participant_discord_id, participant_torn_user_id, status, reserved_until)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (session_id, participant_discord_id)
                DO UPDATE SET status = EXCLUDED.status, participant_torn_user_id = EXCLUDED.participant_torn_user_id, reserved_until = EXCLUDED.reserved_until
                """,
                session_id,
                guild_id,
                discord_id,
                torn_user_id,
                normalized_status,
                reserved_until,
            )
        except Exception as exc:
            _raise_reserved_until_migration_error(exc)

    async def create_or_restore_signup(self, *, session_id: int, guild_id: int, discord_id: int, torn_user_id: Optional[int], reserved_until: Optional[datetime] = None) -> None:
        async with self.pool.acquire() as conn:
            await self._create_or_restore_signup_on_conn(
                conn,
                session_id=session_id,
                guild_id=guild_id,
                discord_id=discord_id,
                torn_user_id=torn_user_id,
                status="reserved",
                reserved_until=reserved_until,
            )

    async def cancel_signup(self, *, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE jump_99k_signups SET status = 'cancelled' WHERE session_id = $1 AND participant_discord_id = $2 RETURNING id", session_id, discord_id)
            return row is not None

    async def list_signups(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *, participant_discord_id AS discord_id, participant_torn_user_id AS torn_user_id
                FROM jump_99k_signups
                WHERE session_id = $1
                ORDER BY is_priority DESC, id ASC
                """,
                session_id,
            )
            return [dict(r) for r in rows]

    async def upsert_readiness_snapshot(self, *, session_id: int, guild_id: int, discord_id: int, energy: int, energy_max: int, drug_cooldown: int, booster_cooldown: int | None, status_text: str) -> None:
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO jump_99k_readiness (session_id, guild_id, discord_id, energy, energy_max, drug_cooldown, booster_cooldown, status_text, checked_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                    ON CONFLICT (session_id, guild_id, discord_id)
                    DO UPDATE SET energy=EXCLUDED.energy, energy_max=EXCLUDED.energy_max, drug_cooldown=EXCLUDED.drug_cooldown, booster_cooldown=EXCLUDED.booster_cooldown, status_text=EXCLUDED.status_text, checked_at=NOW()
                    """,
                    session_id, guild_id, discord_id, energy, energy_max, drug_cooldown, booster_cooldown, status_text,
                )
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_READINESS_MIGRATION_HINT)

    async def list_signups_with_readiness(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.*, s.participant_discord_id AS discord_id, s.participant_torn_user_id AS torn_user_id, r.energy, r.energy_max, r.drug_cooldown, r.booster_cooldown, r.status_text, r.checked_at
                FROM jump_99k_signups s
                LEFT JOIN jump_99k_readiness r ON r.session_id=s.session_id AND r.guild_id=s.guild_id AND r.discord_id=s.participant_discord_id
                WHERE s.session_id=$1
                ORDER BY s.signed_up_at ASC
                """,
                session_id,
            )
            return [dict(r) for r in rows]

    async def list_roster_signups_with_readiness(self, session_id: int) -> list[dict]:
        """Roster participants: paid/verified only, excluding cancelled/unpaid reservations."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    s.*, s.participant_discord_id AS discord_id, s.participant_torn_user_id AS torn_user_id,
                    r.energy, r.energy_max, r.drug_cooldown, r.booster_cooldown, r.status_text, r.checked_at
                FROM jump_99k_signups s
                LEFT JOIN jump_99k_readiness r
                    ON r.session_id=s.session_id
                   AND r.guild_id=s.guild_id
                   AND r.discord_id=s.participant_discord_id
                WHERE s.session_id=$1
                  AND s.payment_verified=TRUE
                  AND s.status IN ('signed_up', 'completed', 'not_completed')
                ORDER BY s.is_priority DESC, s.id ASC
                """,
                session_id,
            )
            return [dict(r) for r in rows]


    async def get_jump_progress(self, session_id: int) -> dict:
        async with self.pool.acquire() as conn:
            try:
                session_row = await conn.fetchrow(
                    """
                    SELECT host_jump_state, host_jump_started_at, host_jump_ended_at
                    FROM jump_99k_sessions
                    WHERE id = $1
                    """,
                    session_id,
                )
                if not session_row:
                    return {"host": {"state": "waiting", "started_at": None, "ended_at": None}, "signups": []}

                signup_rows = await conn.fetch(
                    """
                    SELECT
                        s.id,
                        s.participant_discord_id AS discord_id,
                        COALESCE(NULLIF(s.jump_state, ''), 'waiting') AS state,
                        s.jump_started_at AS started_at,
                        s.jump_ended_at AS ended_at
                    FROM jump_99k_signups s
                    WHERE s.session_id = $1
                      AND s.payment_verified = TRUE
                      AND s.status IN ('signed_up', 'completed', 'not_completed')
                    ORDER BY s.is_priority DESC, s.id ASC
                    """,
                    session_id,
                )
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_JUMP_PROGRESS_MIGRATION_HINT)
                return {"host": {"state": "waiting", "started_at": None, "ended_at": None}, "signups": []}

        return {
            "host": {
                "state": str(session_row.get("host_jump_state") or "waiting"),
                "started_at": session_row.get("host_jump_started_at"),
                "ended_at": session_row.get("host_jump_ended_at"),
            },
            "signups": [dict(row) for row in signup_rows],
        }

    async def run_jump_transition_by_position(self, *, session_id: int, position: int, action: str, actor_discord_id: int) -> tuple[bool, str]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"start", "end"}:
            return False, "Invalid action."
        if int(position) < 1:
            return False, "Invalid roster position."

        async with self.pool.acquire() as conn:
            try:
                async with conn.transaction():
                    session = await conn.fetchrow(
                        """
                        SELECT id,
                               COALESCE(NULLIF(host_jump_state, ''), 'waiting') AS host_jump_state,
                               host_jump_started_at,
                               host_jump_ended_at
                        FROM jump_99k_sessions
                        WHERE id = $1
                          AND status = 'open'
                        FOR UPDATE
                        """,
                        session_id,
                    )
                    if not session:
                        return False, "Session not found."

                    signup_rows = await conn.fetch(
                        """
                        SELECT id,
                               participant_discord_id AS discord_id,
                               COALESCE(NULLIF(jump_state, ''), 'waiting') AS jump_state,
                               jump_started_at,
                               jump_ended_at
                        FROM jump_99k_signups
                        WHERE session_id = $1
                          AND payment_verified = TRUE
                          AND status IN ('signed_up', 'completed', 'not_completed')
                        ORDER BY is_priority DESC, id ASC
                        FOR UPDATE
                        """,
                        session_id,
                    )

                    roster_size = 1 + len(signup_rows)
                    if int(position) > roster_size:
                        return False, f"Position {int(position)} is outside the active roster."

                    states = [str(session.get("host_jump_state") or "waiting")]
                    states.extend(str(r.get("jump_state") or "waiting") for r in signup_rows)

                    in_progress_pos = next((idx for idx, state in enumerate(states, start=1) if state == "in_progress"), None)

                    if normalized_action == "start":
                        if in_progress_pos is not None:
                            return False, "A jump is already in progress. End it first."
                        next_waiting_pos = next((idx for idx, state in enumerate(states, start=1) if state == "waiting"), None)
                        if next_waiting_pos is None:
                            return False, "All roster positions are already done."
                        if int(position) != int(next_waiting_pos):
                            return False, f"Only Start {int(next_waiting_pos)} is allowed right now."

                        if int(position) == 1:
                            await conn.execute(
                            """
                            UPDATE jump_99k_sessions
                            SET host_jump_state = 'in_progress',
                                host_jump_started_at = COALESCE(host_jump_started_at, NOW()),
                                host_jump_ended_at = NULL,
                                signups_locked = TRUE,
                                signups_locked_at = COALESCE(signups_locked_at, NOW()),
                                signups_locked_by_discord_id = COALESCE(signups_locked_by_discord_id, $2),
                                updated_at = NOW()
                            WHERE id = $1
                            """,
                            session_id,
                            actor_discord_id,
                        )
                        else:
                            target_signup = signup_rows[int(position) - 2]
                            await conn.execute(
                            """
                            UPDATE jump_99k_signups
                            SET jump_state = 'in_progress',
                                jump_started_at = COALESCE(jump_started_at, NOW()),
                                jump_ended_at = NULL
                            WHERE id = $1
                            """,
                            int(target_signup["id"]),
                        )

                        return True, f"Started position {int(position)}."

                    if in_progress_pos is None:
                        return False, "No jump is currently in progress."
                    if int(position) != int(in_progress_pos):
                        return False, f"Only End {int(in_progress_pos)} is allowed right now."

                    if int(position) == 1:
                        await conn.execute(
                        """
                        UPDATE jump_99k_sessions
                        SET host_jump_state = 'done',
                            host_jump_started_at = COALESCE(host_jump_started_at, NOW()),
                            host_jump_ended_at = NOW(),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        session_id,
                    )
                    else:
                        target_signup = signup_rows[int(position) - 2]
                        await conn.execute(
                            """
                            UPDATE jump_99k_signups
                            SET jump_state = 'done',
                                jump_started_at = COALESCE(jump_started_at, NOW()),
                                jump_ended_at = NOW()
                            WHERE id = $1
                            """,
                            int(target_signup["id"]),
                        )

                    return True, f"Ended position {int(position)}."
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_JUMP_PROGRESS_MIGRATION_HINT)
                return False, "Jump progress columns are missing. Ask an admin to run migration 2026_02_18_add_99k_jump_progress.sql."

    async def cancel_expired_unpaid(self) -> int:
        async with self.pool.acquire() as conn:
            try:
                result = await conn.execute(
                    """
                    UPDATE jump_99k_signups
                    SET status='cancelled'
                    WHERE status='reserved'
                      AND payment_verified=FALSE
                      AND reserved_until IS NOT NULL
                      AND reserved_until <= NOW()
                    """
                )
            except Exception as exc:
                _raise_reserved_until_migration_error(exc)
            return int(str(result).split()[-1])

    async def list_pending_payment_signups(self, *, limit: int = 50) -> list[dict]:
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(
                    """
                    SELECT s.*, ses.price_item, ses.price_amount, ses.host_discord_id, ses.created_at
                         , ses.priority_increment, ses.priority_enabled
                    FROM jump_99k_signups s
                    JOIN jump_99k_sessions ses ON ses.id = s.session_id
                    WHERE ses.status='open'
                      AND s.status='reserved'
                      AND s.payment_verified=FALSE
                      AND s.reserved_until IS NOT NULL
                      AND s.reserved_until > NOW()
                    ORDER BY s.reserved_until ASC
                    LIMIT $1
                    """,
                    limit,
                )
            except Exception as exc:
                _raise_reserved_until_migration_error(exc)
            return [dict(row) for row in rows]

    async def set_priority_enabled(self, *, session_id: int, enabled: bool) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jump_99k_sessions SET priority_enabled = $2, updated_at = NOW() WHERE id = $1 RETURNING id",
                session_id,
                enabled,
            )
            return row is not None

    async def reserve_priority(self, *, session_id: int, buyer_discord_id: int, signup_id: int, ttl_seconds: int = 300) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT public.jump_99k_reserve_priority($1, $2, $3, $4)",
                session_id,
                str(int(buyer_discord_id)),
                signup_id,
                ttl_seconds,
            )
            return bool(result)

    async def finalize_priority(self, *, session_id: int, buyer_discord_id: int, signup_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT public.jump_99k_finalize_priority($1, $2, $3)",
                session_id,
                str(int(buyer_discord_id)),
                signup_id,
            )
            return bool(result)

    async def lock_signups(self, session_id: int, *, by_discord_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET signups_locked = TRUE,
                    signups_locked_at = COALESCE(signups_locked_at, NOW()),
                    signups_locked_by_discord_id = COALESCE(signups_locked_by_discord_id, $2),
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
                by_discord_id,
            )

    async def manual_add_as_verified_signup(
        self,
        *,
        session_id: int,
        guild_id: int,
        user_discord_id: int,
        added_by_discord_id: int,
        torn_user_id: int | None,
        torn_name: str | None,
        reason: str | None,
    ) -> tuple[bool, str]:
        normalized_reason = str(reason).strip() if reason is not None else None
        if normalized_reason == "":
            normalized_reason = None

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    """
                    SELECT id, guild_id, status, max_slots
                    FROM jump_99k_sessions
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    session_id,
                )
                if not session or str(session.get("status") or "").lower() != "open":
                    return False, "Session is not open."

                existing_signup = await conn.fetchrow(
                    """
                    SELECT id
                    FROM jump_99k_signups
                    WHERE session_id = $1
                      AND participant_discord_id = $2
                      AND status IN ('signed_up', 'completed', 'not_completed')
                    LIMIT 1
                    """,
                    session_id,
                    user_discord_id,
                )
                if existing_signup:
                    return False, "User is already in this session."

                max_slots = int(session.get("max_slots") or 0)
                if max_slots > 0:
                    current_slots = int(
                        await conn.fetchval(
                            """
                            SELECT COUNT(*)
                            FROM jump_99k_signups
                            WHERE session_id = $1
                              AND status IN ('signed_up', 'completed', 'not_completed')
                            """,
                            session_id,
                        )
                        or 0
                    )
                    if current_slots >= max_slots:
                        return False, "This session is full."

                await self._create_or_restore_signup_on_conn(
                    conn,
                    session_id=session_id,
                    guild_id=guild_id,
                    discord_id=user_discord_id,
                    torn_user_id=torn_user_id,
                    status="signed_up",
                    reserved_until=None,
                )

                column_rows = await conn.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'jump_99k_signups'
                      AND column_name = ANY($1::text[])
                    """,
                    [
                        "payment_verified",
                        "payment_verified_at",
                        "reserved_until",
                        "participant_torn_name",
                        "payment_source",
                        "added_manually",
                        "added_by_discord_id",
                        "manual_reason",
                        "manual_added_at",
                    ],
                )
                existing_columns = {str(row["column_name"]) for row in column_rows}

                set_parts: list[str] = []
                params: list[Any] = [session_id, user_discord_id]
                next_param = 3

                if "payment_verified" in existing_columns:
                    set_parts.append("payment_verified = TRUE")
                if "payment_verified_at" in existing_columns:
                    set_parts.append("payment_verified_at = NOW()")
                if "reserved_until" in existing_columns:
                    set_parts.append("reserved_until = NULL")
                if "participant_torn_name" in existing_columns:
                    set_parts.append(f"participant_torn_name = ${next_param}")
                    params.append(torn_name)
                    next_param += 1
                if "payment_source" in existing_columns:
                    set_parts.append("payment_source = 'manual'")
                if "added_manually" in existing_columns:
                    set_parts.append("added_manually = TRUE")
                if "added_by_discord_id" in existing_columns:
                    set_parts.append(f"added_by_discord_id = ${next_param}")
                    params.append(added_by_discord_id)
                    next_param += 1
                if "manual_reason" in existing_columns:
                    set_parts.append(f"manual_reason = ${next_param}")
                    params.append(normalized_reason)
                    next_param += 1
                if "manual_added_at" in existing_columns:
                    set_parts.append("manual_added_at = NOW()")

                if set_parts:
                    await conn.execute(
                        f"""
                        UPDATE jump_99k_signups
                        SET {", ".join(set_parts)}
                        WHERE session_id = $1
                          AND participant_discord_id = $2
                        """,
                        *params,
                    )

        return True, f"Added <@{int(user_discord_id)}> to the session."

    async def create_manual_signup(
        self,
        *,
        session_id: int,
        user_discord_id: int,
        added_by_discord_id: int,
        torn_id: int | None,
        torn_name: str | None,
        reason: str | None,
    ) -> tuple[bool, str]:
        session = await self.get_session(session_id)
        if not session:
            return False, "Session is not open."
        return await self.manual_add_as_verified_signup(
            session_id=session_id,
            guild_id=int(session.get("guild_id") or 0),
            user_discord_id=user_discord_id,
            added_by_discord_id=added_by_discord_id,
            torn_user_id=torn_id,
            torn_name=torn_name,
            reason=reason,
        )

    async def mark_signup_payment_verified(self, *, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            column_rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'jump_99k_signups'
                  AND column_name = ANY($1::text[])
                """,
                ["payment_source"],
            )
            existing_columns = {str(row["column_name"]) for row in column_rows}

            set_parts = [
                "payment_verified=true",
                "payment_verified_at=NOW()",
                "status='signed_up'",
                "reserved_until=NULL",
            ]
            if "payment_source" in existing_columns:
                set_parts.append("payment_source='auto'")

            row = await conn.fetchrow(
                f"UPDATE jump_99k_signups SET {', '.join(set_parts)} WHERE session_id=$1 AND participant_discord_id=$2 RETURNING id",
                session_id,
                discord_id,
            )
            return row is not None

    async def mark_signup_overdose(
        self,
        *,
        session_id: int,
        guild_id: int,
        discord_id: int,
        torn_log_id: str,
        event_timestamp: int,
        meta_json: dict[str, Any],
    ) -> bool:
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    UPDATE jump_99k_signups
                    SET overdose_flag=true,
                        overdose_detected_at=NOW(),
                        overdose_meta=$5::jsonb
                    WHERE session_id=$1
                      AND guild_id=$2
                      AND participant_discord_id=$3
                      AND COALESCE(overdose_flag, FALSE) = FALSE
                    RETURNING id
                    """,
                    session_id,
                    guild_id,
                    discord_id,
                    torn_log_id,
                    json.dumps(
                        {
                            "torn_log_id": str(torn_log_id),
                            "event_timestamp": int(event_timestamp),
                            **(meta_json or {}),
                        },
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                )
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_OVERDOSE_MIGRATION_HINT)
                return False
            return row is not None

    async def get_selected_insurer_for_signup(self, *, session_id: int, discord_id: int) -> Optional[int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT claimed_by_discord_id
                FROM jump_99k_insurance_requests
                WHERE session_id=$1
                  AND participant_discord_id=$2
                  AND claimed_by_discord_id IS NOT NULL
                  AND status = 'completed'
                ORDER BY COALESCE(claimed_at, requested_at) DESC
                LIMIT 1
                """,
                session_id,
                discord_id,
            )
            if not row:
                return None
            return int(row["claimed_by_discord_id"])

    async def close_session_and_record(self, *, session_id: int, guild_id: int, completed_discord_ids: list[int], not_completed_discord_ids: list[int]) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("UPDATE jump_99k_sessions SET status = 'closed', ends_at = NOW(), updated_at = NOW() WHERE id = $1 AND status = 'open' RETURNING id", session_id)
                if not row:
                    return False
                if completed_discord_ids:
                    await conn.execute("UPDATE jump_99k_signups SET status = 'completed' WHERE session_id = $1 AND participant_discord_id = ANY($2::bigint[])", session_id, completed_discord_ids)
                if not_completed_discord_ids:
                    await conn.execute("UPDATE jump_99k_signups SET status = 'not_completed' WHERE session_id = $1 AND participant_discord_id = ANY($2::bigint[])", session_id, not_completed_discord_ids)
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
                guild_id, insurer_discord_id, display_name, policy_summary, contact_instructions, json.dumps(metadata or {}, separators=(",", ":"), ensure_ascii=False),
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


    async def get_insurance_request_for_signup(self, *, session_id: int, participant_discord_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM jump_99k_insurance_requests
                WHERE session_id=$1 AND participant_discord_id=$2
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                session_id,
                participant_discord_id,
            )
            return dict(row) if row else None

    async def mark_insurance_payment_verified(self, *, request_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jump_99k_insurance_requests
                SET status='completed', completed_at=NOW()
                WHERE id=$1 AND status IN ('accepted','claimed')
                RETURNING id
                """,
                request_id,
            )
            return row is not None

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


    async def is_blacklisted(self, guild_id: int, discord_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM blacklist
                WHERE guild_id = $1 AND discord_id = $2 AND (expires_at IS NULL OR expires_at > NOW())
                LIMIT 1
                """,
                guild_id,
                discord_id,
            )
            return dict(row) if row else None

    async def get_signup(self, session_id: int, discord_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *, participant_discord_id AS discord_id, participant_torn_user_id AS torn_user_id
                FROM jump_99k_signups
                WHERE session_id = $1 AND participant_discord_id = $2
                LIMIT 1
                """,
                session_id,
                discord_id,
            )
            return dict(row) if row else None

    async def list_readiness(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(
                    "SELECT session_id, guild_id, discord_id, energy, energy_max, drug_cooldown, booster_cooldown, status_text, checked_at FROM jump_99k_readiness WHERE session_id = $1",
                    session_id,
                )
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                log.error(_READINESS_MIGRATION_HINT)
                return []
            return [dict(r) for r in rows]

    async def update_session_status(self, session_id: int, status: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jump_99k_sessions SET status = $2, updated_at = NOW() WHERE id = $1 RETURNING id",
                session_id,
                status,
            )
            return row is not None

    async def list_non_open_sessions_with_private_channel(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM jump_99k_sessions
                WHERE status IN ('closed', 'cancelled', 'expired', 'completed')
                  AND private_channel_id IS NOT NULL
                  AND cleaned_at IS NULL
                """
            )
            return [dict(r) for r in rows]


    async def list_non_open_sessions_for_cleanup(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM jump_99k_sessions
                WHERE status IN ('closed', 'cancelled', 'expired', 'completed')
                  AND cleaned_at IS NULL
                """
            )
            return [dict(r) for r in rows]

    async def mark_cleaned(self, session_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE jump_99k_sessions SET cleaned_at = NOW(), updated_at = NOW() WHERE id = $1", session_id)

    async def list_open_sessions_by_guild(self, guild_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM jump_99k_sessions WHERE guild_id = $1 AND status = 'open' ORDER BY created_at DESC", guild_id)
            return [dict(r) for r in rows]

    async def set_host_controls_message(self, session_id: int, *, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jump_99k_sessions
                SET host_controls_channel_id = $2,
                    host_controls_message_id = $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_id,
                channel_id,
                message_id,
            )

    async def list_active_sessions_with_roster_panel(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id,
                       guild_id,
                       host_discord_id,
                       private_channel_id,
                       roster_channel_id,
                       roster_message_id,
                       status,
                       signups_locked,
                       signups_locked_at,
                       signups_locked_by_discord_id
                FROM jump_99k_sessions
                WHERE status = 'open'
                  AND roster_message_id IS NOT NULL
                  AND roster_channel_id IS NOT NULL
                """
            )
            return [dict(r) for r in rows]

    async def list_active_sessions_with_roster_panels(self) -> list[dict]:
        """Backwards-compatible alias for callers still using the plural method name."""
        return await self.list_active_sessions_with_roster_panel()

    async def list_open_sessions_with_announcement_panels(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, guild_id, announce_channel_id, announce_message_id, max_slots, status, signups_locked, signups_locked_at, signups_locked_by_discord_id
                FROM jump_99k_sessions
                WHERE status = 'open'
                  AND announce_channel_id IS NOT NULL
                  AND announce_message_id IS NOT NULL
                """
            )
            return [dict(r) for r in rows]


    async def upsert_host_application(self, *, guild_id: int, discord_id: int, torn_user_id: int, torn_name: Optional[str], display_name: Optional[str], forum_url: str, application_data: dict[str, Any]) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO host_applications
                    (guild_id, discord_id, torn_user_id, torn_name, display_name, forum_url, application_data, approval_status, approved_by, approved_at, denial_reason)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7::jsonb, 'pending', NULL, NULL, NULL)
                ON CONFLICT (guild_id, discord_id) DO UPDATE SET
                    torn_user_id = EXCLUDED.torn_user_id,
                    torn_name = EXCLUDED.torn_name,
                    display_name = EXCLUDED.display_name,
                    forum_url = EXCLUDED.forum_url,
                    application_data = EXCLUDED.application_data,
                    approval_status = 'pending',
                    approved_by = NULL,
                    approved_at = NULL,
                    denial_reason = NULL
                RETURNING *
                """,
                guild_id, discord_id, torn_user_id, torn_name, display_name, forum_url, json.dumps(application_data or {}, separators=(",", ":"), ensure_ascii=False),
            )
            return dict(row)

    async def review_host_application(self, *, application_id: int, decision: str, admin_discord_id: int, reason: Optional[str] = None) -> Optional[dict]:
        status = 'approved' if decision == 'approve' else 'rejected'
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE host_applications
                SET approval_status = $2, approved_by = $3, approved_at = NOW(), denial_reason = CASE WHEN $2='rejected' THEN $4 ELSE NULL END
                WHERE id = $1
                RETURNING *
                """,
                application_id, status, admin_discord_id, reason,
            )
            return dict(row) if row else None

    async def list_pending_host_applications(self, guild_id: Optional[int] = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if guild_id is None:
                rows = await conn.fetch("SELECT id, discord_id, guild_id FROM host_applications WHERE approval_status = 'pending'")
            else:
                rows = await conn.fetch("SELECT id, discord_id, guild_id FROM host_applications WHERE approval_status = 'pending' AND guild_id = $1", guild_id)
            return [dict(r) for r in rows]

    async def get_guild_statistics(self, guild_id: int) -> dict:
        async with self.pool.acquire() as conn:
            total = int(await conn.fetchval("SELECT COUNT(*) FROM jump_99k_sessions WHERE guild_id = $1", guild_id) or 0)
            open_count = int(await conn.fetchval("SELECT COUNT(*) FROM jump_99k_sessions WHERE guild_id = $1 AND status = 'open'", guild_id) or 0)
            signups = int(await conn.fetchval("SELECT COUNT(*) FROM jump_99k_signups WHERE guild_id = $1", guild_id) or 0)
            return {"total_sessions": total, "open_sessions": open_count, "total_signups": signups}
