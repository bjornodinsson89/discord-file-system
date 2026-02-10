"""Repository helpers for guild settings CRUD operations."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


class GuildSettingsRepository:
    """CRUD helpers with safe, schema-aligned upserts for per-guild settings."""

    ALLOWED_FIELDS = {
        "announce_channel_id",
        "jump_99k_channel_id",
        "raffle_channel_id",
        "insurance_channel_id",
        "welcome_channel_id",
        "admin_role_ids",
        "host99k_role_id",
        "insurer_role_id",
        "welcome_enabled",
        "welcome_message_template",
        "auto_complete_enabled",
        "reservation_timeout_minutes",
    }
    BIGINT_FIELDS = {
        "announce_channel_id",
        "jump_99k_channel_id",
        "raffle_channel_id",
        "insurance_channel_id",
        "welcome_channel_id",
        "host99k_role_id",
        "insurer_role_id",
    }
    DEFAULT_KEYS = {
        "guild_id": None,
        "announce_channel_id": None,
        "jump_99k_channel_id": None,
        "raffle_channel_id": None,
        "insurance_channel_id": None,
        "welcome_channel_id": None,
        "admin_role_ids": None,
        "host99k_role_id": None,
        "insurer_role_id": None,
        "welcome_enabled": False,
        "welcome_message_template": None,
        "auto_complete_enabled": True,
        "reservation_timeout_minutes": 5,
    }

    def __init__(self, db_manager):
        self._db = db_manager

    @staticmethod
    def _normalize_admin_role_ids(admin_role_ids: Optional[Iterable[Any]]) -> Optional[list[int]]:
        if admin_role_ids is None:
            return None
        normalized: list[int] = []
        for role_id in admin_role_ids:
            if role_id is None:
                continue
            normalized.append(int(role_id))
        return normalized

    @staticmethod
    def _normalize_bigint(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        return int(value)

    def _normalize_updates(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        unknown = set(fields) - self.ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"Unsupported guild settings field(s): {', '.join(sorted(unknown))}")

        normalized: Dict[str, Any] = {}
        for key, value in fields.items():
            if key in self.BIGINT_FIELDS:
                normalized[key] = self._normalize_bigint(value)
                continue
            if key == "admin_role_ids":
                normalized[key] = self._normalize_admin_role_ids(value)
                continue
            if key == "reservation_timeout_minutes" and value is not None:
                normalized[key] = int(value)
                continue
            normalized[key] = value
        return normalized

    def _merge_defaults(self, row: Optional[dict[str, Any]], guild_id: int) -> dict[str, Any]:
        data = dict(self.DEFAULT_KEYS)
        data["guild_id"] = guild_id
        if row:
            data.update(row)
        return data

    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        row = await self._db.create_or_update_guild_settings(guild_id)
        return self._merge_defaults(dict(row) if row else None, guild_id)

    async def get_or_create(self, guild_id: int) -> Dict[str, Any]:
        return await self.get_settings(guild_id)

    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        return await self.get_settings(guild_id)

    async def upsert_settings(self, guild_id: int, **fields: Any) -> Dict[str, Any]:
        if not fields:
            return await self.get_settings(guild_id)

        normalized = self._normalize_updates(fields)
        row = await self._db.update_guild_settings(guild_id, **normalized)
        if row is None:
            row = await self._db.get_guild_settings(guild_id)
        return self._merge_defaults(dict(row) if row else None, guild_id)

    async def upsert(self, guild_id: int, **fields: Any) -> Dict[str, Any]:
        return await self.upsert_settings(guild_id, **fields)

    async def upsert_guild_settings(self, guild_id: int, updates_dict: Dict[str, Any] | None = None, **fields: Any) -> Dict[str, Any]:
        updates = dict(updates_dict or {})
        updates.update(fields)
        return await self.upsert_settings(guild_id, **updates)

    async def set_announce_channel(self, guild_id: int, announce_channel_id: int) -> Dict[str, Any]:
        return await self.upsert_settings(guild_id, announce_channel_id=announce_channel_id)

    @staticmethod
    def resolve_admin_role_ids(settings: Dict[str, Any]) -> list[int]:
        raw_value = settings.get("admin_role_ids")
        if raw_value is None:
            return []
        if isinstance(raw_value, list):
            values = raw_value
        else:
            values = [raw_value]

        normalized: list[int] = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, int):
                normalized.append(value)
                continue
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.isdigit():
                    normalized.append(int(stripped))
            
        return normalized
