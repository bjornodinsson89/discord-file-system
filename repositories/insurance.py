from __future__ import annotations

from datetime import datetime
import json
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
                json.dumps(application_data or {}, separators=(",", ":"), ensure_ascii=False),
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
                SELECT c.coverage_id, c.policy_id, c.user_discord_id, c.user_torn_id,
                       c.xanax_covered, c.premium_paid, c.premium_type, c.payout_amount,
                       c.status, c.expires_at, c.activated_at, c.created_at, c.reserved_until,
                       c.last_log_timestamp, p.guild_id
                FROM insurance_coverage c
                JOIN insurance_policies p ON p.policy_id = c.policy_id
                WHERE c.status = 'active'
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
                json.dumps(payout_items or [], separators=(",", ":"), ensure_ascii=False),
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


    async def get_approved_providers_for_browser(self, guild_id: int) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM insurance_providers WHERE guild_id = $1 AND approval_status = 'approved'", guild_id)
            return [dict(r) for r in rows]

    async def get_provider_policies_for_browser(self, provider_id: int) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM insurance_policies WHERE provider_id = $1 AND active = TRUE", provider_id)
            return [dict(r) for r in rows]

    async def create_coverage(self, *, policy_id: int, user_discord_id: int, user_torn_id: int, xanax_covered: int, premium_paid: int, premium_type: str, payout_amount: int, reserved_until: datetime) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO insurance_coverage (policy_id, user_discord_id, user_torn_id, xanax_covered, premium_paid, premium_type, payout_amount, status, reserved_until, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'reserved',$8,NOW())
                RETURNING coverage_id
                """,
                policy_id, user_discord_id, user_torn_id, xanax_covered, premium_paid, premium_type, payout_amount, reserved_until,
            )
            return int(row['coverage_id'])

    async def get_coverage(self, coverage_id: int) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM insurance_coverage WHERE coverage_id = $1", coverage_id)
            return dict(row) if row else None

    async def activate_coverage(self, coverage_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE insurance_coverage SET status='active', activated_at = NOW() WHERE coverage_id=$1 RETURNING coverage_id", coverage_id)
            return row is not None

    async def set_claim_payout_items(self, claim_id: int, payout_items: list[dict[str, Any]], resolved_by: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE insurance_claims SET payout_items=$2::jsonb, resolved_by=$3, resolved_at=NOW() WHERE claim_id=$1 RETURNING claim_id", claim_id, json.dumps(payout_items or [], separators=(",", ":"), ensure_ascii=False), resolved_by)
            return row is not None

    async def mark_claim_paid_with_log(self, claim_id: int, resolved_by: int, payout_log_id: int, payout_log_timestamp: int, payout_log_evidence: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE insurance_claims
                SET status='paid', resolved_by=$2, resolved_at=NOW(), payout_log_id=$3, payout_log_timestamp=to_timestamp($4), payout_log_evidence=$5
                WHERE claim_id=$1
                RETURNING claim_id
                """,
                claim_id, resolved_by, payout_log_id, payout_log_timestamp, payout_log_evidence,
            )
            return row is not None

    async def reject_claim(self, claim_id: int, resolved_by: int, reason: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE insurance_claims SET status='rejected', resolved_by=$2, notes=$3, resolved_at=NOW() WHERE claim_id=$1 RETURNING claim_id", claim_id, resolved_by, reason)
            return row is not None

    async def add_host_rating(self, host_discord_id: int, rater_discord_id: int, session_id: int, rating: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO host_ratings (host_discord_id, rater_discord_id, session_id, rating, created_at)
                VALUES ($1,$2,$3,$4,NOW())
                ON CONFLICT (session_id, rater_discord_id) DO UPDATE SET rating = EXCLUDED.rating
                """,
                host_discord_id, rater_discord_id, session_id, rating,
            )
