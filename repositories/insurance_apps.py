from __future__ import annotations

from typing import Any

from .base import RepositoryBase


class InsuranceAppsRepository(RepositoryBase):
    async def get_open_app(self, guild_id: int, applicant_discord_id: int) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM public.insurance_apps
                WHERE guild_id = $1
                  AND applicant_discord_id = $2
                  AND status IN ('in_progress', 'submitted')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                guild_id,
                applicant_discord_id,
            )
            return dict(row) if row else None

    async def get_by_id(self, app_id: int) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM public.insurance_apps WHERE id = $1", app_id)
            return dict(row) if row else None

    async def create_app(self, guild_id: int, applicant_discord_id: int, application_channel_id: int) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.insurance_apps (guild_id, applicant_discord_id, application_channel_id)
                VALUES ($1, $2, $3)
                RETURNING *
                """,
                guild_id,
                applicant_discord_id,
                application_channel_id,
            )
            return dict(row)

    async def set_summary_message_id(self, app_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE public.insurance_apps SET summary_message_id = $2, updated_at = NOW() WHERE id = $1",
                app_id,
                message_id,
            )

    async def set_admin_inbox_message_id(self, app_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE public.insurance_apps SET admin_inbox_message_id = $2, updated_at = NOW() WHERE id = $1",
                app_id,
                message_id,
            )

    async def advance_answer(self, app_id: int, expected_question: int, answer_text: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow("SELECT * FROM public.insurance_apps WHERE id = $1 FOR UPDATE", app_id)
                if not current:
                    return None
                row = dict(current)
                if row.get("status") != "in_progress" or int(row.get("current_question") or 1) != expected_question:
                    return None

                if expected_question >= 5:
                    updated = await conn.fetchrow(
                        """
                        UPDATE public.insurance_apps
                        SET answers = COALESCE(answers, '{}'::jsonb) || jsonb_build_object($2::text, $3::text),
                            current_question = 5,
                            status = 'submitted',
                            updated_at = NOW()
                        WHERE id = $1
                        RETURNING *
                        """,
                        app_id,
                        f"q{expected_question}",
                        answer_text,
                    )
                else:
                    updated = await conn.fetchrow(
                        """
                        UPDATE public.insurance_apps
                        SET answers = COALESCE(answers, '{}'::jsonb) || jsonb_build_object($2::text, $3::text),
                            current_question = $4,
                            updated_at = NOW()
                        WHERE id = $1
                        RETURNING *
                        """,
                        app_id,
                        f"q{expected_question}",
                        answer_text,
                        expected_question + 1,
                    )
                return dict(updated) if updated else None

    async def set_status(self, app_id: int, status: str, reviewer_id: int | None, reason: str | None) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE public.insurance_apps
                SET status = $2,
                    reviewed_by_discord_id = $3,
                    reviewed_at = NOW(),
                    decision_reason = $4,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                app_id,
                status,
                reviewer_id,
                reason,
            )
            return dict(row) if row else None

    async def close_app(self, app_id: int, reviewer_id: int | None) -> dict[str, Any] | None:
        return await self.set_status(app_id, "closed", reviewer_id, None)

    async def delete_app(self, app_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM public.insurance_apps WHERE id = $1", app_id)
            return result.split(" ")[-1] == "1"
