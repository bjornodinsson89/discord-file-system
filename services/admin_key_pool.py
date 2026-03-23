from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from repositories.users import UsersRepository
from utils import GuildSettingsRepository, get_database, get_security_manager, get_torn_api
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError

log = logging.getLogger("happy_jumper.admin_key_pool")

_INVALID_KEY_MARKERS = ("incorrect key", "invalid key")
_POOL_STRATEGY = "pool"
_SINGLE_STRATEGY = "single"


class AdminKeyPoolService:
    def __init__(self) -> None:
        self._db = get_database()
        self._settings_repo = GuildSettingsRepository(self._db)
        self._users_repo = UsersRepository(self._db.pool)
        self._locks: dict[int, asyncio.Lock] = {}
        self._next_index_by_guild: dict[int, int] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        return lock

    async def get_bank_rates_for_guild(self, guild: discord.Guild) -> dict[str, Any]:
        return await self._call_for_guild(guild, lambda api_key: get_torn_api().get_bank_rates(api_key))

    async def get_shoplifting_for_guild(self, guild: discord.Guild) -> dict[str, Any]:
        return await self._call_for_guild(guild, lambda api_key: get_torn_api().get_shoplifting(api_key))

    async def _call_for_guild(
        self,
        guild: discord.Guild,
        api_call: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        strategy_settings = await self._get_strategy_settings(guild)
        pool = strategy_settings["pool"]
        if not pool:
            raise TornAPIError(str(strategy_settings["empty_message"]))

        async with self._get_lock(guild.id):
            if not pool:
                raise TornAPIError(str(strategy_settings["empty_message"]))

            start_index = 0 if strategy_settings["strategy"] == _SINGLE_STRATEGY else self._next_index_by_guild.get(guild.id, 0) % len(pool)
            ordered_pool = pool[start_index:] + pool[:start_index]
            permission_error: TornAPIPermissionError | None = None
            rate_limit_error: TornAPIRateLimitError | None = None
            transient_error: TornAPIError | None = None

            for offset, entry in enumerate(ordered_pool):
                discord_id = int(entry["discord_id"])
                try:
                    result = await api_call(str(entry["api_key"]))
                except TornAPIPermissionError as exc:
                    permission_error = permission_error or exc
                    log.info(
                        "admin_key_pool.permission_denied guild_id=%s discord_id=%s strategy=%s",
                        guild.id,
                        discord_id,
                        strategy_settings["strategy"],
                    )
                    continue
                except TornAPIRateLimitError as exc:
                    rate_limit_error = rate_limit_error or exc
                    log.info(
                        "admin_key_pool.rate_limited guild_id=%s discord_id=%s strategy=%s",
                        guild.id,
                        discord_id,
                        strategy_settings["strategy"],
                    )
                    continue
                except TornAPIError as exc:
                    if self._is_invalid_key_error(exc):
                        new_count, deleted = await self._users_repo.record_invalid_key_failure(discord_id)
                        log.warning(
                            "admin_key_pool.invalid_key guild_id=%s discord_id=%s strategy=%s fail_count=%s deleted=%s",
                            guild.id,
                            discord_id,
                            strategy_settings["strategy"],
                            new_count,
                            deleted,
                        )
                        if deleted:
                            log.warning(
                                "admin_key_pool.key_removed guild_id=%s discord_id=%s",
                                guild.id,
                                discord_id,
                            )
                        continue
                    transient_error = transient_error or exc
                    log.info(
                        "admin_key_pool.transient_error guild_id=%s discord_id=%s strategy=%s error=%s",
                        guild.id,
                        discord_id,
                        strategy_settings["strategy"],
                        type(exc).__name__,
                    )
                    continue
                except Exception as exc:
                    transient_error = transient_error or TornAPIError("Could not reach Torn right now.")
                    log.warning(
                        "admin_key_pool.unexpected_error guild_id=%s discord_id=%s strategy=%s error_type=%s",
                        guild.id,
                        discord_id,
                        strategy_settings["strategy"],
                        type(exc).__name__,
                        exc_info=True,
                    )
                    continue

                await self._users_repo.reset_invalid_key_failures(discord_id)
                if strategy_settings["strategy"] == _POOL_STRATEGY:
                    success_index = (start_index + offset + 1) % len(pool)
                    self._next_index_by_guild[guild.id] = success_index
                return result

            if strategy_settings["strategy"] == _POOL_STRATEGY:
                self._next_index_by_guild[guild.id] = (start_index + 1) % len(pool)

        if permission_error and rate_limit_error is None and transient_error is None:
            raise permission_error
        if rate_limit_error and transient_error is None:
            raise rate_limit_error
        if transient_error is not None:
            raise transient_error
        if permission_error is not None:
            raise permission_error
        if rate_limit_error is not None:
            raise rate_limit_error
        raise TornAPIError(str(strategy_settings["failure_message"]))

    async def _get_strategy_settings(self, guild: discord.Guild) -> dict[str, Any]:
        settings = await self._settings_repo.get_or_create(guild.id)
        strategy = str(settings.get("admin_key_strategy") or _POOL_STRATEGY).strip().lower()
        if strategy not in {_POOL_STRATEGY, _SINGLE_STRATEGY}:
            strategy = _POOL_STRATEGY
        if strategy == _SINGLE_STRATEGY:
            return await self._build_single_key_pool(guild, settings)
        return await self._build_pooled_key_pool(guild, settings)

    async def _build_pooled_key_pool(self, guild: discord.Guild, settings: dict[str, Any]) -> dict[str, Any]:
        selected_ids = set(await self._settings_repo.list_admin_key_pool_members(guild.id))
        if not selected_ids:
            return {
                "strategy": _POOL_STRATEGY,
                "pool": [],
                "empty_message": "This server has no admin key pool members configured.",
                "failure_message": "No selected pool key is working for this server.",
            }

        eligible_ids = await self._get_eligible_member_ids(guild, settings=settings)
        selected_eligible_ids = selected_ids.intersection(eligible_ids)
        pool = await self._load_key_pool(guild, selected_eligible_ids)
        if not selected_eligible_ids:
            empty_message = "No selected admin key pool member is currently eligible in this server."
        elif not pool:
            empty_message = "No selected pool member has a stored Torn API key."
        else:
            empty_message = "No selected pool member has a usable Torn API key."
        return {
            "strategy": _POOL_STRATEGY,
            "pool": pool,
            "empty_message": empty_message,
            "failure_message": "No selected pool key is working for this server.",
        }

    async def _build_single_key_pool(self, guild: discord.Guild, settings: dict[str, Any]) -> dict[str, Any]:
        selected_id = GuildSettingsRepository._normalize_bigint(settings.get("admin_key_single_discord_id"))
        if not selected_id:
            return {
                "strategy": _SINGLE_STRATEGY,
                "pool": [],
                "empty_message": "No single admin key is configured for this server.",
                "failure_message": "The selected admin key is not working for this server.",
            }

        member = guild.get_member(selected_id)
        if member is None:
            return {
                "strategy": _SINGLE_STRATEGY,
                "pool": [],
                "empty_message": "The selected admin is no longer in this server.",
                "failure_message": "The selected admin key is not working for this server.",
            }

        eligible_ids = await self._get_eligible_member_ids(guild, settings=settings)
        if selected_id not in eligible_ids:
            return {
                "strategy": _SINGLE_STRATEGY,
                "pool": [],
                "empty_message": "The selected admin is no longer eligible for admin key access in this server.",
                "failure_message": "The selected admin key is not working for this server.",
            }

        pool = await self._load_key_pool(guild, {selected_id})
        if not pool:
            return {
                "strategy": _SINGLE_STRATEGY,
                "pool": [],
                "empty_message": "The selected admin has no stored Torn API key.",
                "failure_message": "The selected admin key is not working for this server.",
            }

        return {
            "strategy": _SINGLE_STRATEGY,
            "pool": pool,
            "empty_message": "The selected admin has no stored Torn API key.",
            "failure_message": "The selected admin key is not working for this server.",
        }

    async def _load_key_pool(self, guild: discord.Guild, eligible_ids: set[int]) -> list[dict[str, Any]]:
        if not eligible_ids:
            return []

        rows = await self._users_repo.list_user_api_keys_by_discord_ids(sorted(eligible_ids))
        security = get_security_manager()
        pool: list[dict[str, Any]] = []
        for row in rows:
            discord_id = int(row.get("discord_id") or 0)
            encrypted_key = row.get("encrypted_key")
            if discord_id <= 0 or not encrypted_key:
                continue
            try:
                api_key = security.decrypt_api_key(str(encrypted_key))
            except Exception:
                log.warning(
                    "admin_key_pool.decrypt_failed guild_id=%s discord_id=%s",
                    guild.id,
                    discord_id,
                )
                continue
            pool.append({"discord_id": discord_id, "api_key": api_key})
        return pool

    async def _get_eligible_member_ids(self, guild: discord.Guild, *, settings: dict[str, Any] | None = None) -> set[int]:
        admin_role_ids = await self._load_admin_role_ids(guild.id, settings=settings)
        return self._collect_eligible_ids(guild, admin_role_ids)

    def _collect_eligible_ids(self, guild: discord.Guild, admin_role_ids: set[int]) -> set[int]:
        eligible_ids: set[int] = set()
        members = list(getattr(guild, "members", []) or [])
        owner_id = int(getattr(guild, "owner_id", 0) or 0)
        if owner_id > 0:
            eligible_ids.add(owner_id)
            owner_member = guild.get_member(owner_id)
            if owner_member is not None and owner_member not in members:
                members.append(owner_member)

        for member in members:
            if member is None:
                continue
            member_id = int(getattr(member, "id", 0) or 0)
            if member_id <= 0:
                continue
            if member_id == owner_id:
                eligible_ids.add(member_id)
                continue
            perms = getattr(member, "guild_permissions", None)
            role_ids = {int(getattr(role, "id", 0) or 0) for role in getattr(member, "roles", []) if getattr(role, "id", None)}
            if bool(getattr(perms, "administrator", False)):
                eligible_ids.add(member_id)
                continue
            if bool(getattr(perms, "manage_guild", False)):
                eligible_ids.add(member_id)
                continue
            if admin_role_ids.intersection(role_ids):
                eligible_ids.add(member_id)
        return eligible_ids

    async def _load_admin_role_ids(self, guild_id: int, *, settings: dict[str, Any] | None = None) -> set[int]:
        resolved_settings = settings or await self._settings_repo.get_or_create(guild_id)
        return set(GuildSettingsRepository.resolve_admin_role_ids(resolved_settings))

    @staticmethod
    def _is_invalid_key_error(exc: TornAPIError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _INVALID_KEY_MARKERS)
