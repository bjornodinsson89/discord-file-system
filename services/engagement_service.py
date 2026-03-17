from __future__ import annotations

import re
from datetime import datetime, timezone

from repositories.engagement import EngagementRepository
from services.prize_token_service import PrizeTokenService

_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9 ]")


def required_total_xp(level: int) -> int:
    return 50 * level * level + 50 * level


def level_from_total_xp(total_xp: int) -> int:
    level = 0
    while required_total_xp(level + 1) <= total_xp:
        level += 1
    return level


def message_fingerprint(content: str) -> str:
    text = _WS_RE.sub(" ", (content or "").strip().lower())
    return _NONWORD_RE.sub("", text)


class EngagementService:
    def __init__(self, repo: EngagementRepository, prize_tokens: PrizeTokenService):
        self.repo = repo
        self.prize_tokens = prize_tokens
        self._level_up_callback = None

    def set_level_up_callback(self, callback):
        self._level_up_callback = callback

    async def award_xp(
        self,
        *,
        guild_id: int,
        user_id: int,
        event_name: str,
        source_type: str,
        source_id: str,
        dedupe_key: str,
        xp_delta: int,
        payload: dict | None = None,
        increments: dict[str, int] | None = None,
    ) -> bool:
        applied = await self.repo.insert_event_ledger(
            guild_id=guild_id,
            user_id=user_id,
            event_name=event_name,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key,
            xp_delta=xp_delta,
            payload=payload,
        )
        if not applied:
            return False
        profile = await self.repo.apply_xp_delta(guild_id, user_id, xp_delta, increments or {})
        old_level = int(profile.get("level") or 0)
        new_level = level_from_total_xp(int(profile.get("xp_total") or 0))
        if new_level > old_level:
            await self.repo.update_level(guild_id, user_id, new_level)
            for level in range(old_level + 1, new_level + 1):
                granted = await self.prize_tokens.grant_level_up_token(guild_id, user_id, level)
                if granted and self._level_up_callback is not None:
                    await self._level_up_callback(guild_id, user_id, level)
        return True

    async def process_paid_raffle_purchase(self, payload: dict) -> bool:
        ticket_count = max(0, int(payload.get("ticket_count") or 0))
        xp = min(50, 15 + (2 * ticket_count))
        return await self.award_xp(
            guild_id=int(payload.get("guild_id") or 0),
            user_id=int(payload.get("user_id") or 0),
            event_name="paid_raffle_purchase_verified",
            source_type="raffle",
            source_id=str(payload.get("entry_id") or payload.get("raffle_id") or "0"),
            dedupe_key=str(payload.get("dedupe_key") or ""),
            xp_delta=xp,
            payload=payload,
            increments={
                "paid_raffle_xp_total": xp,
                "paid_raffle_purchases_count": 1,
                "paid_raffle_tickets_count": ticket_count,
            },
        )

    async def process_raffle_prize_token_purchase_confirmed(self, payload: dict) -> bool:
        ticket_count = max(0, int(payload.get("ticket_count") or 0))
        xp = min(50, 15 + (2 * ticket_count))
        return await self.award_xp(
            guild_id=int(payload.get("guild_id") or 0),
            user_id=int(payload.get("user_id") or 0),
            event_name="raffle_prize_token_purchase_confirmed",
            source_type="raffle",
            source_id=str(payload.get("entry_id") or payload.get("raffle_id") or "0"),
            dedupe_key=str(payload.get("dedupe_key") or ""),
            xp_delta=xp,
            payload=payload,
            increments={"paid_raffle_xp_total": xp},
        )

    async def process_jump_purchase_verified(self, payload: dict) -> bool:
        xp = 40
        return await self.award_xp(
            guild_id=int(payload.get("guild_id") or 0),
            user_id=int(payload.get("user_id") or 0),
            event_name="jump_99k_purchase_verified",
            source_type="jump_99k",
            source_id=str(payload.get("signup_id") or payload.get("session_id") or "0"),
            dedupe_key=str(payload.get("dedupe_key") or ""),
            xp_delta=xp,
            payload=payload,
            increments={"jump_purchase_xp_total": xp, "jump_99k_purchases_count": 1},
        )

    async def process_jump_completed(self, payload: dict) -> bool:
        xp = 75
        return await self.award_xp(
            guild_id=int(payload.get("guild_id") or 0),
            user_id=int(payload.get("user_id") or 0),
            event_name="jump_99k_completed",
            source_type="jump_99k",
            source_id=str(payload.get("session_id") or "0"),
            dedupe_key=str(payload.get("dedupe_key") or ""),
            xp_delta=xp,
            payload=payload,
            increments={"jump_completion_xp_total": xp, "jump_99k_completed_count": 1},
        )

    async def message_xp_if_eligible(
        self,
        *,
        guild_id: int,
        user_id: int,
        content: str,
        channel_id: int,
        role_ids: list[int],
        category_id: int | None,
        now: datetime | None = None,
    ) -> bool:
        settings = await self.repo.get_or_create_guild_settings(guild_id)
        if not settings.get("enabled") or not settings.get("message_xp_enabled"):
            return False
        if channel_id in set(settings.get("ignored_channel_ids_json") or []):
            return False
        if category_id and category_id in set(settings.get("ignored_category_ids_json") or []):
            return False
        ignored_roles = set(settings.get("ignored_role_ids_json") or [])
        if any(r in ignored_roles for r in role_ids):
            return False

        trimmed = (content or "").strip()
        if len(trimmed) < 8:
            return False

        state = await self.repo.get_message_state(guild_id, user_id)
        current_fp = message_fingerprint(trimmed)
        if state and current_fp and current_fp == str(state.get("last_message_fingerprint") or ""):
            return False

        now = now or datetime.now(timezone.utc)
        cooldown = int(settings.get("message_xp_cooldown_seconds") or 60)
        last_at = state.get("last_eligible_message_at") if state else None
        if last_at is not None:
            elapsed = (now - last_at).total_seconds()
            if elapsed < cooldown:
                return False

        applied = await self.award_xp(
            guild_id=guild_id,
            user_id=user_id,
            event_name="message",
            source_type="message",
            source_id=str(channel_id),
            dedupe_key=f"message:{guild_id}:{user_id}:{int(now.timestamp()) // cooldown}",
            xp_delta=int(settings.get("message_xp_amount") or 12),
            payload={"channel_id": channel_id},
            increments={"message_xp_total": int(settings.get("message_xp_amount") or 12)},
        )
        if applied:
            await self.repo.upsert_message_state(
                guild_id,
                user_id,
                last_eligible_message_at=now,
                last_message_fingerprint=current_fp,
                last_channel_id=channel_id,
            )
        return applied

    async def reaction_xp_if_eligible(
        self,
        *,
        guild_id: int,
        reactor_user_id: int,
        target_user_id: int,
        message_id: int,
    ) -> bool:
        settings = await self.repo.get_or_create_guild_settings(guild_id)
        if not settings.get("enabled") or not settings.get("reaction_xp_enabled"):
            return False
        cap = int(settings.get("reaction_xp_hourly_cap") or 20)
        awarded = await self.repo.get_hourly_reaction_xp(guild_id, target_user_id)
        xp_amount = int(settings.get("reaction_xp_amount") or 2)
        if awarded + xp_amount > cap:
            return False
        marker = await self.repo.add_reaction_marker(guild_id, message_id, reactor_user_id, target_user_id)
        if not marker:
            return False
        return await self.award_xp(
            guild_id=guild_id,
            user_id=target_user_id,
            event_name="reaction_received",
            source_type="reaction",
            source_id=str(message_id),
            dedupe_key=f"reaction:{guild_id}:{message_id}:{reactor_user_id}",
            xp_delta=xp_amount,
            payload={"reactor_user_id": reactor_user_id},
            increments={"reaction_xp_total": xp_amount},
        )

    async def voice_xp_if_eligible(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        minute_bucket: int,
    ) -> bool:
        settings = await self.repo.get_or_create_guild_settings(guild_id)
        if not settings.get("enabled") or not settings.get("voice_xp_enabled"):
            return False
        ignored_channels = set(settings.get("ignored_channel_ids_json") or [])
        if channel_id in ignored_channels:
            return False
        xp = int(settings.get("voice_xp_per_minute") or 5)
        return await self.award_xp(
            guild_id=guild_id,
            user_id=user_id,
            event_name="voice",
            source_type="voice",
            source_id=str(channel_id),
            dedupe_key=f"voice:{guild_id}:{user_id}:{minute_bucket}",
            xp_delta=xp,
            payload={"channel_id": channel_id, "minute_bucket": minute_bucket},
            increments={"voice_xp_total": xp},
        )
