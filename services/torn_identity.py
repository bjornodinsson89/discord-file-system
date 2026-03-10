from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from repositories.user_torn_identity_cache import UserTornIdentityCacheRepository
from repositories.users import UsersRepository
from utils import get_security_manager, get_torn_api
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError

_NICK_PATTERN = re.compile(r"^(?P<name>.*?)\[(?P<torn_id>\d{1,10})\]\s*$")


@dataclass
class BuyerIdentityResolution:
    torn_user_id: int
    torn_name: str | None
    source: str
    is_official_discord_verified: bool


def parse_member_torn_identity_from_nickname(member: discord.Member | None) -> tuple[int | None, str | None]:
    if member is None:
        return None, None
    raw_value = str(member.nick or member.display_name or "").strip()
    if not raw_value:
        return None, None
    match = _NICK_PATTERN.match(raw_value)
    if not match:
        return None, None
    torn_id = int(match.group("torn_id"))
    name = (match.group("name") or "").strip()
    return torn_id if torn_id > 0 else None, name or None


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
        return BuyerIdentityResolution(buyer_torn_id, torn_name, "api", False), None

    cached = await cache_repo.get_identity(int(guild.id), int(buyer_discord_id))
    if cached and int(cached.get("torn_user_id") or 0) > 0:
        return BuyerIdentityResolution(
            torn_user_id=int(cached["torn_user_id"]),
            torn_name=str(cached.get("torn_name") or "").strip() or None,
            source=str(cached.get("source") or "nickname"),
            is_official_discord_verified=bool(cached.get("is_official_discord_verified")),
        ), None

    creator_key = await users_repo.get_user_api_key(int(creator_discord_id))
    if creator_key and creator_key.get("encrypted_key"):
        try:
            creator_api_key = get_security_manager().decrypt(creator_key["encrypted_key"])
            lookup = await get_torn_api().resolve_torn_user_by_discord_id(
                creator_api_key,
                int(buyer_discord_id),
                audit_discord_id=int(creator_discord_id),
                audit_torn_id=int(creator_key.get("torn_user_id") or 0) or None,
                audit_context="discord_identity_lookup",
                audit_query_meta={"target_discord_id": int(buyer_discord_id)},
            )
            if lookup:
                await cache_repo.upsert_identity(
                    guild_id=int(guild.id),
                    discord_id=int(buyer_discord_id),
                    torn_user_id=int(lookup["torn_user_id"]),
                    torn_name=lookup.get("torn_name"),
                    source="discord_lookup",
                    is_official_discord_verified=bool(lookup.get("is_official_discord_verified")),
                    last_verified_at=datetime.now(timezone.utc),
                )
                return BuyerIdentityResolution(
                    torn_user_id=int(lookup["torn_user_id"]),
                    torn_name=lookup.get("torn_name"),
                    source="discord_lookup",
                    is_official_discord_verified=bool(lookup.get("is_official_discord_verified")),
                ), None
        except TornAPIPermissionError:
            pass
        except TornAPIRateLimitError:
            pass
        except TornAPIError:
            pass
        except Exception:
            pass

    member = guild.get_member(int(buyer_discord_id))
    nickname_torn_id, nickname_name = parse_member_torn_identity_from_nickname(member)
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
        return BuyerIdentityResolution(
            torn_user_id=int(nickname_torn_id),
            torn_name=nickname_name,
            source="nickname",
            is_official_discord_verified=False,
        ), None

    return None, (
        "Could not resolve your Torn identity. Link your Torn API key with the bot, "
        "OR complete official Torn Discord verification, OR use a server nickname like Name [1234567]."
    )
