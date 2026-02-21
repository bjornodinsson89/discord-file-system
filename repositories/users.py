from __future__ import annotations

import logging

import asyncpg

from .base import RepositoryBase

log = logging.getLogger("happy_jumper.repositories.users")


class UsersRepository(RepositoryBase):
    async def get_user_api_key(self, discord_id: int):
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_api_keys WHERE discord_id = $1",
                discord_id,
            )
            return dict(row) if row else None

    async def list_all_user_api_keys(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM user_api_keys
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                """
            )
            return [dict(row) for row in rows]

    async def upsert_user_api_key(
        self,
        *,
        discord_id: int,
        torn_user_id: int,
        torn_name: str | None = None,
        encrypted_key: str,
        timezone_name: str | None = None,
    ) -> None:
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO user_api_keys (discord_id, torn_user_id, torn_name, encrypted_key, timezone_name, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                    ON CONFLICT (discord_id)
                    DO UPDATE SET
                        torn_user_id = EXCLUDED.torn_user_id,
                        torn_name = EXCLUDED.torn_name,
                        encrypted_key = EXCLUDED.encrypted_key,
                        timezone_name = COALESCE(NULLIF(BTRIM(EXCLUDED.timezone_name), ''), user_api_keys.timezone_name),
                        updated_at = NOW()
                    """,
                    discord_id,
                    torn_user_id,
                    torn_name,
                    encrypted_key,
                    timezone_name,
                )
            except asyncpg.UndefinedColumnError as exc:
                error_text = str(exc)
                missing_torn_name = "torn_name" in error_text
                missing_timezone = "timezone_name" in error_text

                if missing_torn_name and not missing_timezone:
                    log.warning(
                        "user_api_keys.torn_name missing; falling back to upsert without torn_name"
                    )
                    await conn.execute(
                        """
                        INSERT INTO user_api_keys (discord_id, torn_user_id, encrypted_key, timezone_name, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, NOW(), NOW())
                        ON CONFLICT (discord_id)
                        DO UPDATE SET
                            torn_user_id = EXCLUDED.torn_user_id,
                            encrypted_key = EXCLUDED.encrypted_key,
                            timezone_name = COALESCE(NULLIF(BTRIM(EXCLUDED.timezone_name), ''), user_api_keys.timezone_name),
                            updated_at = NOW()
                        """,
                        discord_id,
                        torn_user_id,
                        encrypted_key,
                        timezone_name,
                    )
                elif missing_timezone and not missing_torn_name:
                    log.warning(
                        "user_api_keys.timezone_name missing; falling back to upsert without timezone_name"
                    )
                    await conn.execute(
                        """
                        INSERT INTO user_api_keys (discord_id, torn_user_id, torn_name, encrypted_key, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, NOW(), NOW())
                        ON CONFLICT (discord_id)
                        DO UPDATE SET
                            torn_user_id = EXCLUDED.torn_user_id,
                            torn_name = EXCLUDED.torn_name,
                            encrypted_key = EXCLUDED.encrypted_key,
                            updated_at = NOW()
                        """,
                        discord_id,
                        torn_user_id,
                        torn_name,
                        encrypted_key,
                    )
                elif missing_torn_name and missing_timezone:
                    log.warning(
                        "user_api_keys.torn_name and user_api_keys.timezone_name missing; falling back to minimal upsert"
                    )
                    await conn.execute(
                        """
                        INSERT INTO user_api_keys (discord_id, torn_user_id, encrypted_key, created_at, updated_at)
                        VALUES ($1, $2, $3, NOW(), NOW())
                        ON CONFLICT (discord_id)
                        DO UPDATE SET
                            torn_user_id = EXCLUDED.torn_user_id,
                            encrypted_key = EXCLUDED.encrypted_key,
                            updated_at = NOW()
                        """,
                        discord_id,
                        torn_user_id,
                        encrypted_key,
                    )
                else:
                    raise

    async def update_timezone(self, discord_id: int, timezone_name: str) -> None:
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """
                    UPDATE public.user_api_keys
                    SET timezone_name = $2,
                        updated_at = NOW()
                    WHERE discord_id = $1
                    """,
                    discord_id,
                    timezone_name,
                )
            except asyncpg.UndefinedColumnError as exc:
                if "timezone_name" not in str(exc):
                    raise
                raise RuntimeError("Database migration missing: user_api_keys.timezone_name.") from exc

    async def update_torn_identity(self, *, discord_id: int, torn_user_id: int, torn_name: str | None) -> None:
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """
                    UPDATE user_api_keys
                    SET torn_user_id = $2,
                        torn_name = $3,
                        updated_at = NOW()
                    WHERE discord_id = $1
                    """,
                    discord_id,
                    torn_user_id,
                    torn_name,
                )
            except asyncpg.UndefinedColumnError as exc:
                if "torn_name" not in str(exc):
                    raise
                await conn.execute(
                    """
                    UPDATE user_api_keys
                    SET torn_user_id = $2,
                        updated_at = NOW()
                    WHERE discord_id = $1
                    """,
                    discord_id,
                    torn_user_id,
                )

    async def delete_user_api_key(self, discord_id: int) -> bool:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM user_api_keys WHERE discord_id = $1 RETURNING discord_id",
                discord_id,
            )
            return row is not None
