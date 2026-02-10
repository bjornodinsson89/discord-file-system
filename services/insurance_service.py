from __future__ import annotations

import logging

from .errors import InvalidInput, NotFound, BusinessRuleViolation
from .validation import sanitize_text, validate_discord_id, validate_guild_id, validate_torn_id, validate_url

log = logging.getLogger("happy_jumper.services.insurance")


class InsuranceService:
    def __init__(self, db):
        self.db = db

    async def submit_insurer_application(
        self,
        *,
        guild_id: int,
        discord_id: int,
        torn_user_id: int,
        torn_name: str,
        forum_url: str,
        company_name: str | None,
        description_terms_vouches: str,
    ) -> dict:
        validate_guild_id(guild_id)
        validate_discord_id(discord_id)
        validate_torn_id(torn_user_id)
        clean_torn_name = sanitize_text(torn_name, field_name="Torn name", max_length=100)
        clean_forum_url = validate_url(forum_url, required_host_contains="torn.com")
        clean_desc = sanitize_text(description_terms_vouches, field_name="Description", max_length=3000)
        clean_company = (company_name or "").strip()[:255] or None

        try:
            payload = {
                "torn_name": clean_torn_name,
                "forum_url": clean_forum_url,
                "description_terms": clean_desc,
                "proof_vouches": None,
            }
            return await self.db.upsert_insurer_application(
                guild_id=guild_id,
                discord_id=discord_id,
                torn_user_id=torn_user_id,
                company_name=clean_company,
                application_data=payload,
            )
        except Exception:
            log.exception("Submit insurer application failed guild_id=%s discord_id=%s", guild_id, discord_id)
            raise

    async def review_insurer_application(self, *, provider_id: int, decision: str, admin_discord_id: int, reason: str | None = None):
        if decision not in {"approve", "deny"}:
            raise InvalidInput("Invalid decision")
        validate_discord_id(admin_discord_id)
        try:
            result = await self.db.review_insurer_application(provider_id, decision, admin_discord_id, reason=reason)
            if not result:
                raise NotFound("Application not found")
            return result
        except (InvalidInput, NotFound, BusinessRuleViolation):
            raise
        except Exception:
            log.exception("Review insurer application failed provider_id=%s", provider_id)
            raise
