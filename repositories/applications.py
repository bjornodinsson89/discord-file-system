from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from .base import RepositoryBase


class ApplicationsRepository(RepositoryBase):
    async def get_insurer_profile(self, *, guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM insurer_profiles WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            return dict(row) if row else None

    async def get_open_application(self, *, guild_id: int, user_id: Optional[int] = None, user_discord_id: Optional[int] = None, app_type: str) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM applications
                WHERE guild_id = $1
                  AND user_id = $2
                  AND app_type = $3
                  AND status IN ('pending', 'in_progress', 'submitted')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                guild_id,
                int(user_discord_id if user_discord_id is not None else user_id or 0),
                app_type,
            )
            return dict(row) if row else None


    async def create_application(
        self,
        *,
        guild_id: int,
        user_id: int,
        app_type: str,
        application_channel_id: int,
        thread_id: Optional[int],
        channel_id: int,
        summary_message_id: Optional[int] = None,
        answers: Optional[dict[str, Any]] = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO applications (guild_id, user_id, app_type, application_channel_id, thread_id, channel_id, summary_message_id, answers)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING id
                """,
                guild_id,
                user_id,
                app_type,
                application_channel_id,
                thread_id,
                channel_id,
                summary_message_id,
                json.dumps(answers or {}, separators=(",", ":"), ensure_ascii=False),
            )
            return int(row["id"])

    async def set_summary_message_id(self, *, app_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE applications SET summary_message_id = $2, updated_at = NOW() WHERE id = $1",
                app_id,
                message_id,
            )

    async def get_by_thread_id(self, thread_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM applications WHERE thread_id = $1", thread_id)
            return dict(row) if row else None

    async def get_by_application_channel_id(self, channel_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM applications WHERE application_channel_id = $1", channel_id)
            return dict(row) if row else None

    async def get_application_by_id(self, app_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM applications WHERE id = $1", app_id)
            return dict(row) if row else None

    async def get_by_id(self, app_id: int) -> Optional[dict[str, Any]]:
        return await self.get_application_by_id(app_id)

    async def delete_application(self, app_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM applications WHERE id = $1", app_id)
            return result.split(" ")[-1] == "1"

    async def advance_question_if_current(
        self,
        *,
        app_id: int,
        expected_question: int,
        answer_text: str,
        next_status: Optional[str] = None,
        next_question: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        q_key = f"q{expected_question + 1}"
        query = """
            UPDATE applications
            SET answers = COALESCE(answers, '{}'::jsonb) || jsonb_build_object($3::text, $4::text),
                current_question = $5,
                status = COALESCE($6, status),
                updated_at = NOW()
            WHERE id = $1
              AND current_question = $2
              AND status = 'in_progress'
            RETURNING *
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                app_id,
                expected_question,
                q_key,
                answer_text,
                int(next_question if next_question is not None else expected_question + 1),
                next_status,
            )
            return dict(row) if row else None

    async def set_review(
        self,
        *,
        app_id: int,
        expected_status: str,
        new_status: str,
        reviewed_by: int,
        denial_reason: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE applications
                SET status = $3,
                    reviewed_by = $4,
                    reviewed_at = NOW(),
                    denial_reason = $5,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = $2
                RETURNING *
                """,
                app_id,
                expected_status,
                new_status,
                reviewed_by,
                denial_reason,
            )
            return dict(row) if row else None

    async def request_changes(self, *, app_id: int, current_question: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE applications
                SET status = 'in_progress',
                    current_question = $2,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'submitted'
                RETURNING *
                """,
                app_id,
                current_question,
            )
            return dict(row) if row else None

    async def trim_answers_from(self, *, app_id: int, from_question: int) -> None:
        if from_question <= 1:
            keys = [f"q{i}" for i in range(1, 6)]
        else:
            keys = [f"q{i}" for i in range(from_question, 6)]
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE applications SET answers = answers - $2::text[], updated_at = NOW() WHERE id = $1",
                app_id,
                keys,
            )

    async def list_expired_candidates(self, *, older_than: datetime) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM applications
                WHERE status = 'in_progress'
                  AND updated_at < $1
                """,
                older_than,
            )
            return [dict(r) for r in rows]

    async def mark_expired(self, app_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE applications SET status = 'expired', updated_at = NOW() WHERE id = $1 AND status = 'in_progress'",
                app_id,
            )

    async def upsert_insurer_profile(self, *, guild_id: int, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO insurer_profiles (
                    guild_id,
                    user_id,
                    display_name,
                    coverage_summary,
                    pricing_text,
                    rules_exclusions,
                    response_time_text,
                    contact_notes,
                    image_url,
                    activation_delay_minutes,
                    coverage_duration_minutes,
                    updated_at,
                    created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),NOW())
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    coverage_summary = EXCLUDED.coverage_summary,
                    pricing_text = EXCLUDED.pricing_text,
                    rules_exclusions = EXCLUDED.rules_exclusions,
                    response_time_text = EXCLUDED.response_time_text,
                    contact_notes = EXCLUDED.contact_notes,
                    image_url = EXCLUDED.image_url,
                    activation_delay_minutes = EXCLUDED.activation_delay_minutes,
                    coverage_duration_minutes = EXCLUDED.coverage_duration_minutes,
                    updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                user_id,
                data["display_name"],
                data["coverage_summary"],
                data["pricing_text"],
                data["rules_exclusions"],
                data.get("response_time_text"),
                data.get("contact_notes"),
                data.get("image_url"),
                data["activation_delay_minutes"],
                data["coverage_duration_minutes"],
            )
            return dict(row)

    async def upsert_wizard_state(self, *, guild_id: int, user_id: int, step: int, draft: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO insurer_profile_wizards (guild_id, user_id, step, draft, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, NOW())
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET step = EXCLUDED.step, draft = EXCLUDED.draft, updated_at = NOW()
                """,
                guild_id,
                user_id,
                step,
                json.dumps(draft, separators=(",", ":"), ensure_ascii=False),
            )

    async def get_wizard_state(self, *, guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM insurer_profile_wizards WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            return dict(row) if row else None

    async def get_active_wizard_state_for_user(self, *, user_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM insurer_profile_wizards
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                user_id,
            )
            return dict(row) if row else None

    async def clear_wizard_state(self, *, guild_id: int, user_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM insurer_profile_wizards WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )

    async def has_approved_insurer_application(self, *, guild_id: int, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT 1
                FROM applications
                WHERE guild_id = $1
                  AND user_id = $2
                  AND app_type = 'insurer'
                  AND status = 'approved'
                LIMIT 1
                """,
                guild_id,
                user_id,
            )
            return bool(val)
