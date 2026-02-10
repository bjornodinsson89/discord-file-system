from __future__ import annotations

from datetime import datetime
from typing import Optional, Any

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
                INSERT INTO insurance_providers
                    (guild_id, discord_id, torn_user_id, company_name, application_data,
                     approval_status, approved_by, approved_at, denial_reason, created_at, updated_at)
                VALUES
                    ($1, $2, $3, $4, $5::jsonb, 'pending', NULL, NULL, NULL, NOW(), NOW())
                ON CONFLICT (guild_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    company_name = EXCLUDED.company_name,
                    application_data = EXCLUDED.application_data,
                    approval_status = 'pending',
                    approved_by = NULL,
                    approved_at = NULL,
                    denial_reason = NULL,
                    updated_at = NOW()
                RETURNING provider_id
                """,
                guild_id,
                discord_id,
                torn_user_id,
                torn_name,
                application_data,
            )
            return int(row["provider_id"])

    async def resolve_provider_application(self, application_id: int, decision: str, reviewer_discord_id: int, reason: Optional[str] = None) -> bool:
        status = "approved" if decision == "approve" else "rejected"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE insurance_providers
                SET approval_status = $2,
                    approved_by = $3,
                    approved_at = NOW(),
                    denial_reason = CASE WHEN $2 = 'rejected' THEN $4::text ELSE NULL END,
                    updated_at = NOW()
                WHERE provider_id = $1
                  AND approval_status = 'pending'
                RETURNING provider_id
                """,
                application_id,
                status,
                reviewer_discord_id,
                reason,
            )
            return row is not None

    # ============================================================================
    # BACKGROUND WORKER METHODS (Added for events.py compatibility)
    # ============================================================================

    async def expire_coverage(self) -> int:
        """Expire old coverage and return count of expired records."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE insurance_coverage 
                SET status = 'expired' 
                WHERE status = 'active' 
                AND expires_at < NOW()
                """
            )
            # Parse result string like "UPDATE 3" to get count
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0

    async def get_active_coverage(self) -> list[dict[str, Any]]:
        """Get all active coverage records for monitoring."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT coverage_id, policy_id, user_discord_id, user_torn_id, 
                       xanax_covered, premium_paid, premium_type, payout_amount,
                       status, expires_at, activated_at, created_at, reserved_until,
                       last_log_timestamp
                FROM insurance_coverage 
                WHERE status = 'active'
                """
            )
            return [dict(row) for row in rows]

    async def update_coverage_last_check(self, coverage_id: int, timestamp: int) -> None:
        """Update last log timestamp for coverage."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE insurance_coverage 
                SET last_log_timestamp = $1 
                WHERE coverage_id = $2
                """,
                timestamp,
                coverage_id
            )

    async def check_existing_claim(self, coverage_id: int, log_id: int) -> bool:
        """Check if claim already exists for this log."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM insurance_claims 
                WHERE coverage_id = $1 AND torn_log_id = $2
                LIMIT 1
                """,
                coverage_id,
                log_id
            )
            return row is not None

    async def create_claim(
        self,
        coverage_id: int,
        policy_id: int,
        user_discord_id: int,
        provider_id: int,
        claim_type: str,
        xanax_lost: int,
        payout_amount: int,
        payout_items: list,
        torn_log_id: Optional[int] = None,
        torn_log_timestamp: Optional[int] = None,
        torn_log_evidence: Optional[str] = None
    ) -> int:
        """Create insurance claim and return claim_id."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO insurance_claims (
                    coverage_id, policy_id, user_discord_id, provider_id,
                    claim_type, xanax_lost, payout_amount, payout_type, payout_items,
                    torn_log_id, torn_log_timestamp, torn_log_evidence, status,
                    created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, 'pending', $8::jsonb,
                    $9, to_timestamp($10), $11, 'pending', NOW()
                )
                RETURNING claim_id
                """,
                coverage_id,
                policy_id,
                user_discord_id,
                provider_id,
                claim_type,
                xanax_lost,
                payout_amount,
                payout_items,
                torn_log_id,
                torn_log_timestamp,
                torn_log_evidence
            )
            return int(row["claim_id"])

    async def get_policy(self, policy_id: int) -> Optional[dict[str, Any]]:
        """Get policy by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT policy_id, provider_id, guild_id, name, description,
                       covered_jump_types, cost_type, cost_amount, coverage_type,
                       max_coverage_xanax, payout_description, premium_per_xanax,
                       payout_per_xanax, duration_hours, active, payout_items
                FROM insurance_policies
                WHERE policy_id = $1
                """,
                policy_id
            )
            return dict(row) if row else None

    async def get_provider_by_id(self, provider_id: int) -> Optional[dict[str, Any]]:
        """Get provider by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT provider_id, discord_id, torn_user_id, company_name, guild_id,
                       verified, active, approval_status, approved_by, approved_at,
                       created_at, application_data, denial_reason
                FROM insurance_providers
                WHERE provider_id = $1
                """,
                provider_id
            )
            return dict(row) if row else None

    async def get_claim(self, claim_id: int) -> Optional[dict[str, Any]]:
        """Get claim by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT claim_id, coverage_id, policy_id, user_discord_id, provider_id,
                       claim_type, xanax_lost, payout_amount, payout_type, status,
                       torn_log_id, torn_log_evidence, notes, resolved_by, resolved_at,
                       created_at, payout_items, payout_log_id, payout_log_timestamp,
                       payout_log_evidence
                FROM insurance_claims
                WHERE claim_id = $1
                """,
                claim_id
            )
            return dict(row) if row else None

    async def list_pending_insurer_applications(self) -> list[dict[str, Any]]:
        """List all pending insurer applications for persistent views."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT provider_id as application_id, guild_id, discord_id, 
                       torn_user_id, company_name as torn_name, application_data,
                       approval_status, created_at
                FROM insurance_providers
                WHERE approval_status = 'pending'
                """
            )
            return [dict(row) for row in rows]
