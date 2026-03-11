from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from repositories.user_torn_identity_cache import UserTornIdentityCacheRepository
from repositories.users import UsersRepository

_NICK_PATTERN = re.compile(r"^(?P<name>.*?)\[(?P<torn_id>\d{1,10})\]\s*$")
logger = logging.getLogger(__name__)

_IDENTITY_FAILURE_MESSAGE = (
    "Could not resolve your Torn identity. Link your Torn API key with the bot, "
    "or use a server nickname like Name [1234567]."
)


@dataclass
class BuyerIdentityResolution:
    torn_user_id: int
    torn_name: str | None
    source: str
    is_official_discord_verified: bool


def parse_member_torn_identity_from_nickname(member: discord.Member | None) -> tuple[int | None, str | None]:
    if member is None:
        return None, None
    values = [str(member.nick or "").strip(), str(member.display_name or "").strip()]
    for raw_value in values:
        if not raw_value:
            continue
        match = _NICK_PATTERN.match(raw_value)
        if not match:
            continue
        torn_id = int(match.group("torn_id"))
        name = (match.group("name") or "").strip()
        return torn_id if torn_id > 0 else None, name or None
    return None, None


async def resolve_casino_deposit_identity(
    *,
    guild: discord.Guild,
    buyer_discord_id: int,
    db,
) -> tuple[BuyerIdentityResolution | None, str | None]:
    users_repo = UsersRepository(db.pool)
    cache_repo = UserTornIdentityCacheRepository(db.pool)

    buyer_key = await users_repo.get_user_api_key(int(buyer_discord_id))
    buyer_torn_id = int((buyer_key or {}).get("torn_user_id") or 0)
    if buyer_torn_id > 0:
        torn_name = str((buyer_key or {}).get("torn_name") or "").strip() or None
        await cache_repo.upsert_identity(
            guild_id=int(guild.id),
            discord_id=int(buyer_discord_id),
            torn_user_id=buyer_torn_id,
            torn_name=torn_name,
            source="api",
            is_official_discord_verified=False,
            last_verified_at=datetime.now(timezone.utc),
        )
        return (
            BuyerIdentityResolution(
                torn_user_id=buyer_torn_id,
                torn_name=torn_name,
                source="api",
                is_official_discord_verified=False,
            ),
            None,
        )

    cached = await cache_repo.get_identity(int(guild.id), int(buyer_discord_id), trusted_only=True)
    member = guild.get_member(int(buyer_discord_id))
    nickname_torn_id, nickname_name = parse_member_torn_identity_from_nickname(member)

    if cached and int(cached.get("torn_user_id") or 0) > 0:
        cached_torn_id = int(cached["torn_user_id"])
        if nickname_torn_id and int(nickname_torn_id) != cached_torn_id:
            logger.warning(
                "casino.deposit.identity_conflict guild_id=%s discord_user_id=%s cached_torn_id=%s nickname_torn_id=%s",
                int(guild.id),
                int(buyer_discord_id),
                int(cached_torn_id),
                int(nickname_torn_id),
            )
            return None, (
                "Could not safely resolve your Torn identity. Link your Torn API key with the bot, "
                "or update your server nickname to match your linked Torn account."
            )

        if nickname_torn_id and (nickname_name or None) and int(nickname_torn_id) == cached_torn_id:
            await cache_repo.upsert_identity(
                guild_id=int(guild.id),
                discord_id=int(buyer_discord_id),
                torn_user_id=int(nickname_torn_id),
                torn_name=nickname_name,
                source=str(cached.get("source") or "nickname"),
                is_official_discord_verified=bool(cached.get("is_official_discord_verified")),
                last_verified_at=cached.get("last_verified_at"),
            )

        return (
            BuyerIdentityResolution(
                torn_user_id=cached_torn_id,
                torn_name=str(cached.get("torn_name") or "").strip() or None,
                source=str(cached.get("source") or "nickname"),
                is_official_discord_verified=bool(cached.get("is_official_discord_verified")),
            ),
            None,
        )

    if nickname_torn_id:
        await cache_repo.upsert_identity(
            guild_id=int(guild.id),
            discord_id=int(buyer_discord_id),
            torn_user_id=int(nickname_torn_id),
            torn_name=nickname_name,
            source="nickname",
            is_official_discord_verified=False,
            last_verified_at=None,
        )
        return (
            BuyerIdentityResolution(
                torn_user_id=int(nickname_torn_id),
                torn_name=nickname_name,
                source="nickname",
                is_official_discord_verified=False,
            ),
            None,
        )

    return None, _IDENTITY_FAILURE_MESSAGE


async def resolve_buyer_identity_for_paid_feature(
    *,
    guild: discord.Guild,
    buyer_discord_id: int,
    creator_discord_id: int,
    db,
) -> tuple[BuyerIdentityResolution | None, str | None]:
    users_repo = UsersRepository(db.pool)
    cache_repo = UserTornIdentityCacheRepository(db.pool)

    buyer_key = await users_repo.get_user_api_key(int(buyer_discord_id))
    creator_torn_id: int | None = None

    async def _creator_torn_id() -> int | None:
        nonlocal creator_torn_id
        if creator_torn_id is not None:
            return creator_torn_id
        creator_torn_id = 0
        creator_key = await users_repo.get_user_api_key(int(creator_discord_id))
        if creator_key:
            creator_torn_id = int((creator_key or {}).get("torn_user_id") or 0)
        return creator_torn_id if creator_torn_id > 0 else None

    async def _safe_resolution_or_error(
        *, torn_user_id: int, torn_name: str | None, source: str, is_official_discord_verified: bool
    ) -> tuple[BuyerIdentityResolution | None, str | None]:
        resolved_torn_id = int(torn_user_id or 0)
        if resolved_torn_id <= 0:
            return None, _IDENTITY_FAILURE_MESSAGE
        if int(buyer_discord_id) != int(creator_discord_id):
            creator_id = await _creator_torn_id()
            if creator_id and resolved_torn_id == creator_id:
                return None, (
                    "Could not safely resolve your Torn identity. Link your Torn API key with the bot, "
                    "or use a server nickname like Name [1234567]."
                )
        return (
            BuyerIdentityResolution(
                torn_user_id=resolved_torn_id,
                torn_name=torn_name,
                source=source,
                is_official_discord_verified=is_official_discord_verified,
            ),
            None,
        )

    buyer_torn_id = int((buyer_key or {}).get("torn_user_id") or 0)
    if buyer_torn_id > 0:
        torn_name = str((buyer_key or {}).get("torn_name") or "").strip() or None
        safe_resolution, safe_error = await _safe_resolution_or_error(
            torn_user_id=buyer_torn_id,
            torn_name=torn_name,
            source="api",
            is_official_discord_verified=False,
        )
        if not safe_resolution:
            return None, safe_error
        await cache_repo.upsert_identity(
            guild_id=int(guild.id),
            discord_id=int(buyer_discord_id),
            torn_user_id=buyer_torn_id,
            torn_name=torn_name,
            source="api",
            is_official_discord_verified=False,
            last_verified_at=datetime.now(timezone.utc),
        )
        return safe_resolution, None

    cached = await cache_repo.get_identity(int(guild.id), int(buyer_discord_id), trusted_only=True)
    if cached and int(cached.get("torn_user_id") or 0) > 0:
        safe_resolution, safe_error = await _safe_resolution_or_error(
            torn_user_id=int(cached["torn_user_id"]),
            torn_name=str(cached.get("torn_name") or "").strip() or None,
            source=str(cached.get("source") or "nickname"),
            is_official_discord_verified=bool(cached.get("is_official_discord_verified")),
        )
        if safe_resolution:
            return safe_resolution, None
        return None, safe_error

    member = guild.get_member(int(buyer_discord_id))
    nickname_torn_id, nickname_name = parse_member_torn_identity_from_nickname(member)
    if nickname_torn_id:
        safe_resolution, safe_error = await _safe_resolution_or_error(
            torn_user_id=int(nickname_torn_id),
            torn_name=nickname_name,
            source="nickname",
            is_official_discord_verified=False,
        )
        if not safe_resolution:
            return None, safe_error
        await cache_repo.upsert_identity(
            guild_id=int(guild.id),
            discord_id=int(buyer_discord_id),
            torn_user_id=int(nickname_torn_id),
            torn_name=nickname_name,
            source="nickname",
            is_official_discord_verified=False,
            last_verified_at=None,
        )
        return safe_resolution, None

    return None, _IDENTITY_FAILURE_MESSAGE
