"""Repository helpers for guild settings CRUD operations."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Optional


logger = logging.getLogger(__name__)


def _jsonb(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    if isinstance(v, str):
        return v
    return json.dumps(v)


class GuildSettingsRepository:
    """CRUD helpers with safe, schema-aligned upserts for per-guild settings."""

    ALLOWED_FIELDS = {
        "announce_channel_id",
        "jump_99k_channel_id",
        "jump_announce_channel_id",
        "raffle_channel_id",
        "raffle_announcement_channel_id",
        "raffle_purchase_channel_id",
        "raffle_giveaway_purchase_channel_id",
        "pool_channel_id",
        "pools_post_channel_id",
        "raffle_announce_enabled",
        "disable_99k_announcements",
        "insurance_channel_id",
        "applications_category_id",
        "applications_admin_inbox_channel_id",
        "host_apps_admin_inbox_channel_id",
        "insurance_apps_admin_inbox_channel_id",
        "welcome_channel_id",
        "admin_role_ids",
        "jump_ping_role_ids",
        "host99k_role_id",
        "insurer_role_id",
        "welcome_enabled",
        "welcome_message_template",
        "auto_complete_enabled",
        "reservation_timeout_minutes",
        "default_max_slots",
        "host_tax_enabled",
        "host_tax_recipient_torn_id",
        "host_tax_type",
        "host_tax_item_id",
        "host_tax_quantity",
        "host_tax_cash_amount",
    }
    BIGINT_FIELDS = {
        "announce_channel_id",
        "jump_99k_channel_id",
        "jump_announce_channel_id",
        "raffle_channel_id",
        "raffle_announcement_channel_id",
        "raffle_purchase_channel_id",
        "raffle_giveaway_purchase_channel_id",
        "insurance_channel_id",
        "applications_category_id",
        "applications_admin_inbox_channel_id",
        "host_apps_admin_inbox_channel_id",
        "insurance_apps_admin_inbox_channel_id",
        "welcome_channel_id",
        "pool_channel_id",
        "pools_post_channel_id",
        "host99k_role_id",
        "insurer_role_id",
        "default_max_slots",
        "host_tax_cash_amount",
    }
    BOOLEAN_FIELDS = {
        "welcome_enabled",
        "raffle_announce_enabled",
        "auto_complete_enabled",
        "host_tax_enabled",
        "disable_99k_announcements",
    }
    DEFAULT_KEYS = {
        "guild_id": None,
        "announce_channel_id": None,
        "jump_99k_channel_id": None,
        "jump_announce_channel_id": None,
        "raffle_channel_id": None,
        "insurance_channel_id": None,
        "applications_category_id": None,
        "applications_admin_inbox_channel_id": None,
        "host_apps_admin_inbox_channel_id": None,
        "insurance_apps_admin_inbox_channel_id": None,
        "raffle_announcement_channel_id": None,
        "raffle_purchase_channel_id": None,
        "raffle_giveaway_purchase_channel_id": None,
        "welcome_channel_id": None,
        "pool_channel_id": None,
        "pools_post_channel_id": None,
        "admin_role_ids": None,
        "jump_ping_role_ids": [],
        "host99k_role_id": None,
        "insurer_role_id": None,
        "welcome_enabled": False,
        "raffle_announce_enabled": True,
        "disable_99k_announcements": False,
        "welcome_message_template": None,
        "auto_complete_enabled": True,
        "reservation_timeout_minutes": 5,
        "default_max_slots": 5,
        "host_tax_enabled": False,
        "host_tax_recipient_torn_id": None,
        "host_tax_type": None,
        "host_tax_item_id": None,
        "host_tax_quantity": None,
        "host_tax_cash_amount": None,
    }

    def __init__(self, db_manager):
        self._db = db_manager

    @staticmethod
    def _try_parse_json(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped[0] not in "[{":
            return value
        try:
            return json.loads(stripped)
        except Exception:
            return value

    @classmethod
    def _normalize_admin_role_ids(
        cls,
        admin_role_ids: Optional[Iterable[Any]],
        *,
        guild_id: Optional[int] = None,
        field_name: str = "admin_role_ids",
    ) -> Optional[list[int]]:
        if admin_role_ids is None:
            return None

        admin_role_ids = cls._try_parse_json(admin_role_ids)

        if admin_role_ids in ("", []):
            return []

        if isinstance(admin_role_ids, (int, str)):
            stripped = admin_role_ids.strip() if isinstance(admin_role_ids, str) else admin_role_ids
            if isinstance(stripped, str) and "," in stripped:
                candidate_values = [part.strip() for part in stripped.split(",")]
            else:
                candidate_values = [admin_role_ids]
        elif isinstance(admin_role_ids, dict):
            candidate_values = admin_role_ids.values()
        else:
            candidate_values = admin_role_ids

        normalized: list[int] = []
        for role_id in candidate_values:
            if role_id is None or role_id == "":
                continue
            try:
                normalized.append(int(str(role_id).strip()))
            except (TypeError, ValueError):
                logger.warning(
                    "Failed to normalize role id value for guild settings",
                    extra={
                        "guild_id": guild_id,
                        "field_name": field_name,
                        "raw_value_type": type(admin_role_ids).__name__,
                        "bad_value_type": type(role_id).__name__,
                    },
                )
                return []
        return normalized

    @staticmethod
    def _normalize_bigint(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        return int(value)

    @staticmethod
    def _normalize_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return bool(value)

    @classmethod
    def _normalize_role_id_list(
        cls,
        role_ids: Optional[Iterable[Any]],
        *,
        guild_id: Optional[int] = None,
        field_name: str = "jump_ping_role_ids",
    ) -> list[int]:
        if role_ids is None:
            return []

        role_ids = cls._try_parse_json(role_ids)

        if role_ids in ("", []):
            return []

        if isinstance(role_ids, (int, str)):
            stripped = role_ids.strip() if isinstance(role_ids, str) else role_ids
            if isinstance(stripped, str) and "," in stripped:
                candidate_values = [part.strip() for part in stripped.split(",")]
            else:
                candidate_values = [role_ids]
        elif isinstance(role_ids, dict):
            candidate_values = role_ids.values()
        else:
            candidate_values = role_ids

        normalized: list[int] = []
        for role_id in candidate_values:
            if role_id is None or role_id == "":
                continue
            try:
                normalized.append(int(str(role_id).strip()))
            except (TypeError, ValueError):
                logger.warning(
                    "Failed to normalize role id list for guild settings",
                    extra={
                        "guild_id": guild_id,
                        "field_name": field_name,
                        "raw_value_type": type(role_ids).__name__,
                        "bad_value_type": type(role_id).__name__,
                    },
                )
                return []
        return sorted(set(normalized))

    def _normalize_updates(self, fields: Dict[str, Any], *, guild_id: Optional[int] = None) -> Dict[str, Any]:
        unknown = set(fields) - self.ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"Unsupported guild settings field(s): {', '.join(sorted(unknown))}")

        normalized: Dict[str, Any] = {}
        for key, value in fields.items():
            if key in self.BIGINT_FIELDS:
                normalized[key] = self._normalize_bigint(value)
                continue
            if key in self.BOOLEAN_FIELDS:
                normalized[key] = self._normalize_bool(value)
                continue
            if key == "admin_role_ids":
                normalized_ids = self._normalize_admin_role_ids(value, guild_id=guild_id, field_name=key)
                if normalized_ids is None:
                    normalized[key] = None
                else:
                    unique_int_ids = sorted(set(normalized_ids))
                    normalized[key] = unique_int_ids
                continue
            if key == "jump_ping_role_ids":
                normalized[key] = self._normalize_role_id_list(value, guild_id=guild_id, field_name=key)
                continue
            if key in {"reservation_timeout_minutes", "default_max_slots", "host_tax_recipient_torn_id", "host_tax_item_id", "host_tax_quantity"} and value is not None:
                normalized_value = int(value)
                if key == "default_max_slots" and not 1 <= normalized_value <= 7:
                    raise ValueError("default_max_slots must be between 1 and 7")
                normalized[key] = normalized_value
                continue
            normalized[key] = value
        return normalized

    def _merge_defaults(self, row: Optional[dict[str, Any]], guild_id: int) -> dict[str, Any]:
        data = dict(self.DEFAULT_KEYS)
        data["guild_id"] = guild_id
        if row:
            data.update(row)
        for field in self.BIGINT_FIELDS:
            data[field] = self._normalize_bigint(data.get(field))
        for field in self.BOOLEAN_FIELDS:
            data[field] = self._normalize_bool(data.get(field))
        data["admin_role_ids"] = self._normalize_admin_role_ids(
            data.get("admin_role_ids"), guild_id=guild_id, field_name="admin_role_ids"
        )
        data["jump_ping_role_ids"] = self._normalize_role_id_list(
            data.get("jump_ping_role_ids"), guild_id=guild_id, field_name="jump_ping_role_ids"
        )
        return data


    async def _db_insert_or_get_settings(self, guild_id: int) -> Optional[dict[str, Any]]:
        if not hasattr(self._db, "pool"):
            return None
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.guild_settings (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id) DO UPDATE SET guild_id = EXCLUDED.guild_id
                RETURNING *
                """,
                guild_id,
            )
            return dict(row) if row else None

    async def _db_insert_settings(self, guild_id: int, fields: Dict[str, Any]) -> Optional[dict[str, Any]]:
        if not hasattr(self._db, "pool"):
            return None
        async with self._db.pool.acquire() as conn:
            columns = ["guild_id"]
            placeholders = ["$1"]
            values: list[Any] = [guild_id]

            for index, (key, value) in enumerate(fields.items(), start=2):
                columns.append(key)
                if key in {"admin_role_ids", "jump_ping_role_ids"}:
                    placeholders.append(f"${index}::jsonb")
                    values.append(_jsonb(value))
                else:
                    placeholders.append(f"${index}")
                    values.append(value)

            row = await conn.fetchrow(
                f"""
                INSERT INTO public.guild_settings ({', '.join(columns)})
                VALUES ({', '.join(placeholders)})
                ON CONFLICT (guild_id) DO NOTHING
                RETURNING *
                """,
                *values,
            )
            if row:
                return dict(row)

            existing = await conn.fetchrow("SELECT * FROM public.guild_settings WHERE guild_id = $1", guild_id)
            return dict(existing) if existing else None

    async def _db_update_settings(self, guild_id: int, fields: Dict[str, Any]) -> Optional[dict[str, Any]]:
        if not hasattr(self._db, "pool"):
            return None
        async with self._db.pool.acquire() as conn:
            sets = []
            values = []
            for i, (key, value) in enumerate(fields.items(), 1):
                if key in {"admin_role_ids", "jump_ping_role_ids"}:
                    sets.append(f"{key} = ${i}::jsonb")
                    values.append(_jsonb(value))
                else:
                    sets.append(f"{key} = ${i}")
                    values.append(value)
            values.append(guild_id)
            row = await conn.fetchrow(
                f"UPDATE public.guild_settings SET {', '.join(sets)} WHERE guild_id = ${len(values)} RETURNING *",
                *values,
            )
            return dict(row) if row else None

    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        if hasattr(self._db, "create_or_update_guild_settings"):
            row = await self._db.create_or_update_guild_settings(guild_id)
            return self._merge_defaults(dict(row) if row else None, guild_id)

        row = None
        if hasattr(self._db, "get_guild_settings"):
            row = await self._db.get_guild_settings(guild_id)
        if row:
            return self._merge_defaults(dict(row), guild_id)

        if hasattr(self._db, "update_guild_settings"):
            maybe = await self._db.update_guild_settings(guild_id)
            if maybe:
                return self._merge_defaults(dict(maybe), guild_id)

        fallback = await self._db_insert_or_get_settings(guild_id)
        return self._merge_defaults(fallback, guild_id)

    async def get_or_create(self, guild_id: int) -> Dict[str, Any]:
        return await self.get_settings(guild_id)

    async def insert_or_get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        return await self.get_settings(guild_id)

    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        return await self.get_settings(guild_id)

    async def create_default_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        defaults = {
            "admin_role_ids": [],
            "jump_ping_role_ids": [],
            "announce_channel_id": None,
            "jump_99k_channel_id": None,
            "jump_announce_channel_id": None,
            "raffle_channel_id": None,
            "raffle_announcement_channel_id": None,
            "raffle_purchase_channel_id": None,
            "raffle_giveaway_purchase_channel_id": None,
            "insurance_channel_id": None,
            "applications_category_id": None,
            "applications_admin_inbox_channel_id": None,
            "host_apps_admin_inbox_channel_id": None,
            "insurance_apps_admin_inbox_channel_id": None,
            "welcome_channel_id": None,
            "pool_channel_id": None,
            "pools_post_channel_id": None,
            "host99k_role_id": None,
            "insurer_role_id": None,
            "welcome_enabled": False,
            "raffle_announce_enabled": True,
            "disable_99k_announcements": False,
            "welcome_message_template": None,
            "auto_complete_enabled": True,
            "reservation_timeout_minutes": 5,
            "default_max_slots": 5,
            "host_tax_enabled": False,
            "host_tax_recipient_torn_id": None,
            "host_tax_type": None,
            "host_tax_item_id": None,
            "host_tax_quantity": None,
            "host_tax_cash_amount": None,
        }
        row = await self._db_insert_settings(guild_id, defaults)
        return self._merge_defaults(row, guild_id)

    async def ensure_guild_exists(self, guild_id: int):
        if not hasattr(self._db, "pool"):
            return
        async with self._db.pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT guild_id FROM public.guild_settings WHERE guild_id = $1", guild_id)
        if existing:
            return
        await self.create_default_guild_settings(guild_id)

    async def upsert_settings(self, guild_id: int, **fields: Any) -> Dict[str, Any]:
        if not fields:
            return await self.get_settings(guild_id)

        normalized = self._normalize_updates(fields, guild_id=guild_id)
        row = None
        if hasattr(self._db, "update_guild_settings"):
            row = await self._db.update_guild_settings(guild_id, **normalized)
            if row is None and hasattr(self._db, "get_guild_settings"):
                row = await self._db.get_guild_settings(guild_id)
        else:
            await self._db_insert_or_get_settings(guild_id)
            row = await self._db_update_settings(guild_id, normalized)
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
    def resolve_raffle_giveaway_purchase_channel_id(settings: Dict[str, Any]) -> Optional[int]:
        return GuildSettingsRepository._normalize_bigint(settings.get("raffle_giveaway_purchase_channel_id"))

    @staticmethod
    def resolve_admin_role_ids(settings: Dict[str, Any]) -> list[int]:
        normalized = GuildSettingsRepository._normalize_admin_role_ids(settings.get("admin_role_ids"))
        return [] if normalized is None else normalized
