from __future__ import annotations

from typing import Optional

from .base import RepositoryBase


class InsuranceRepository(RepositoryBase):
    async def upsert_provider_application(
        self,
        guild_id: int,
        discord_id: int,
        torn_user_id: int,
        torn_name: Optional[str],
        display_name: Optional[str],
        forum_url: Optional[str],
        application_data: dict,
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO insurer_applications
                    (guild_id, discord_id, torn_user_id, torn_name, display_name, forum_url, application_data,
                     approval_status, approved_by, approved_at, denial_reason)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7::jsonb, 'pending', NULL, NULL, NULL)
                ON CONFLICT (guild_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    torn_name = EXCLUDED.torn_name,
                    display_name = EXCLUDED.display_name,
                    forum_url = EXCLUDED.forum_url,
                    application_data = EXCLUDED.application_data,
                    approval_status = 'pending',
                    approved_by = NULL,
                    approved_at = NULL,
                    denial_reason = NULL,
                    updated_at = NOW()
                RETURNING application_id
                """,
                guild_id,
                discord_id,
                torn_user_id,
                torn_name,
                display_name,
                forum_url,
                application_data,
            )
            return int(row["application_id"])

    async def resolve_provider_application(self, application_id: int, decision: str, reviewer_discord_id: int, reason: Optional[str] = None) -> bool:
        status = "approved" if decision == "approve" else "rejected"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE insurer_applications
                SET approval_status = $2,
                    approved_by = $3,
                    approved_at = NOW(),
                    denial_reason = CASE WHEN $2 = 'rejected' THEN $4::text ELSE NULL END,
                    updated_at = NOW()
                WHERE application_id = $1
                  AND approval_status = 'pending'
                RETURNING application_id
                """,
                application_id,
                status,
                reviewer_discord_id,
                reason,
            )
            return row is not None
