"""Shared service for insurer/host99k application review actions."""

from __future__ import annotations

from typing import Optional, Dict, Any

from utils import get_database
from services import InsuranceService, InvalidInput


async def perform_application_review(
    *,
    category: str,
    application_id: int,
    decision: str,
    admin_discord_id: int,
    reason: Optional[str] = None,
    guild_id_hint: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Apply an application decision and write audit logs."""
    db = get_database()

    if category == "insurer":
        result = await InsuranceService(db).review_insurer_application(
            provider_id=application_id,
            decision=decision,
            admin_discord_id=admin_discord_id,
            reason=reason,
        )
        if not result:
            return None

        guild_id = result.get("guild_id") or guild_id_hint
        action = "insurer_application_approved" if decision == "approve" else "insurer_application_denied"
        target_type = "insurance_provider"
    elif category == "host99k":
        result = await db.review_host_application(
            application_id=application_id,
            decision=decision,
            admin_discord_id=admin_discord_id,
            reason=reason,
        )
        if not result:
            return None

        guild_id = result.get("guild_id") or guild_id_hint
        action = "host_application_approved" if decision == "approve" else "host_application_denied"
        target_type = "host99k"
    else:
        raise ValueError(f"Unsupported category: {category}")

    await db.log_audit(
        actor_id=admin_discord_id,
        action=action,
        target_type=target_type,
        target_id=application_id,
        payload={
            "category": category,
            "application_id": application_id,
            "applicant_discord_id": result.get("discord_id"),
            "approved_by": admin_discord_id,
            "reason": reason,
        },
        guild_id=guild_id,
        source="discord",
    )

    return {
        "result": result,
        "guild_id": guild_id,
        "applicant_discord_id": result.get("discord_id"),
        "action": action,
    }
