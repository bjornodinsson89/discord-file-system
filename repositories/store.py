from __future__ import annotations

from typing import Any

import asyncpg

from .base import RepositoryBase


class StoreRepository(RepositoryBase):
    async def upsert_guild_settings(self, guild_id: int, **changes: Any) -> dict:
        async with self.acquire() as conn:
            return await self.upsert_guild_settings_with_conn(conn, guild_id, **changes)

    async def upsert_guild_settings_with_conn(self, conn: asyncpg.Connection, guild_id: int, **changes: Any) -> dict:
        if changes:
            assignments = [f"{key} = ${idx}" for idx, key in enumerate(changes.keys(), start=2)]
            await conn.execute(
                f"""
                INSERT INTO store_guild_settings (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id) DO UPDATE
                SET {', '.join(assignments)}, updated_at = NOW()
                """,
                guild_id,
                *changes.values(),
            )
        else:
            await conn.execute(
                """
                INSERT INTO store_guild_settings (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                guild_id,
            )
        row = await conn.fetchrow("SELECT * FROM store_guild_settings WHERE guild_id = $1", guild_id)
        return dict(row)

    async def get_or_create_guild_settings(self, guild_id: int) -> dict:
        async with self.acquire() as conn:
            return await self.upsert_guild_settings_with_conn(conn, guild_id)

    async def create_item(self, **payload: Any) -> dict:
        async with self.acquire() as conn:
            return await self.create_item_with_conn(conn, **payload)

    async def create_item_with_conn(self, conn: asyncpg.Connection, **payload: Any) -> dict:
        row = await conn.fetchrow(
            """
            INSERT INTO reward_store_items (
                guild_id, name, description, category, token_cost, stock, is_active,
                fulfillment_type, discord_role_id, torn_item_name, torn_item_id,
                thumbnail_url, max_per_user, requires_admin_approval, created_by
            ) VALUES (
                $1,$2,$3,$4,$5,$6,COALESCE($7, TRUE),$8,$9,$10,$11,$12,$13,COALESCE($14, TRUE),$15
            )
            RETURNING *
            """,
            int(payload["guild_id"]),
            str(payload["name"]),
            payload.get("description"),
            str(payload["category"]),
            int(payload["token_cost"]),
            payload.get("stock"),
            payload.get("is_active", True),
            str(payload["fulfillment_type"]),
            payload.get("discord_role_id"),
            payload.get("torn_item_name"),
            payload.get("torn_item_id"),
            payload.get("thumbnail_url"),
            payload.get("max_per_user"),
            payload.get("requires_admin_approval", True),
            int(payload["created_by"]),
        )
        return dict(row)

    async def get_item(self, guild_id: int, item_id: int, *, for_update: bool = False, conn: asyncpg.Connection | None = None) -> dict | None:
        if conn is None:
            async with self.acquire() as local_conn:
                return await self.get_item(guild_id, item_id, for_update=for_update, conn=local_conn)
        clause = " FOR UPDATE" if for_update else ""
        row = await conn.fetchrow(
            f"SELECT * FROM reward_store_items WHERE guild_id = $1 AND id = $2{clause}",
            guild_id,
            item_id,
        )
        return dict(row) if row else None

    async def list_items(self, guild_id: int, *, category: str | None = None, active_only: bool = True) -> list[dict]:
        async with self.acquire() as conn:
            where = ["guild_id = $1"]
            params: list[Any] = [guild_id]
            idx = 2
            if category:
                where.append(f"category = ${idx}")
                params.append(category)
                idx += 1
            if active_only:
                where.append("is_active = TRUE")
            rows = await conn.fetch(
                f"SELECT * FROM reward_store_items WHERE {' AND '.join(where)} ORDER BY id DESC",
                *params,
            )
            return [dict(r) for r in rows]


    async def list_all_guild_items(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM reward_store_items WHERE guild_id = $1 ORDER BY id ASC",
                guild_id,
            )
            return [dict(r) for r in rows]

    async def get_storefront_items(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM reward_store_items WHERE guild_id = $1 AND is_active = TRUE ORDER BY id ASC",
                guild_id,
            )
            return [dict(r) for r in rows]

    async def update_item(self, guild_id: int, item_id: int, **changes: Any) -> dict | None:
        if not changes:
            return await self.get_item(guild_id, item_id)
        async with self.acquire() as conn:
            sets = [f"{k} = ${idx}" for idx, k in enumerate(changes.keys(), start=3)]
            await conn.execute(
                f"UPDATE reward_store_items SET {', '.join(sets)}, updated_at = NOW() WHERE guild_id = $1 AND id = $2",
                guild_id,
                item_id,
                *changes.values(),
            )
            return await self.get_item(guild_id, item_id, conn=conn)

    async def adjust_stock(self, guild_id: int, item_id: int, delta: int, *, conn: asyncpg.Connection | None = None) -> dict | None:
        if conn is None:
            async with self.acquire() as local_conn:
                async with local_conn.transaction():
                    return await self.adjust_stock(guild_id, item_id, delta, conn=local_conn)
        row = await conn.fetchrow(
            """
            UPDATE reward_store_items
            SET stock = CASE
                WHEN stock IS NULL THEN NULL
                ELSE stock + $3
            END,
            updated_at = NOW()
            WHERE guild_id = $1 AND id = $2
            RETURNING *
            """,
            guild_id,
            item_id,
            delta,
        )
        return dict(row) if row else None

    async def count_user_redemptions_for_item(self, guild_id: int, user_id: int, store_item_id: int, *, conn: asyncpg.Connection | None = None) -> int:
        if conn is None:
            async with self.acquire() as local_conn:
                return await self.count_user_redemptions_for_item(guild_id, user_id, store_item_id, conn=local_conn)
        count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM reward_redemptions
            WHERE guild_id = $1
              AND user_id = $2
              AND store_item_id = $3
              AND status IN ('pending', 'fulfilled')
            """,
            guild_id,
            user_id,
            store_item_id,
        )
        return int(count or 0)

    async def create_redemption(self, *, conn: asyncpg.Connection | None = None, **payload: Any) -> dict:
        if conn is None:
            async with self.acquire() as local_conn:
                return await self.create_redemption(conn=local_conn, **payload)
        row = await conn.fetchrow(
            """
            INSERT INTO reward_redemptions (
                guild_id, user_id, store_item_id, token_cost, quantity, status,
                fulfillment_type, notes, fulfilled_by, fulfilled_at,
                admin_message_channel_id, admin_message_id
            ) VALUES ($1,$2,$3,$4,COALESCE($5, 1),$6,$7,$8,$9,$10,$11,$12)
            RETURNING *
            """,
            int(payload["guild_id"]),
            int(payload["user_id"]),
            int(payload["store_item_id"]),
            int(payload["token_cost"]),
            payload.get("quantity", 1),
            str(payload["status"]),
            str(payload["fulfillment_type"]),
            payload.get("notes"),
            payload.get("fulfilled_by"),
            payload.get("fulfilled_at"),
            payload.get("admin_message_channel_id"),
            payload.get("admin_message_id"),
        )
        return dict(row)

    async def get_redemption(self, guild_id: int, redemption_id: int, *, conn: asyncpg.Connection | None = None, for_update: bool = False) -> dict | None:
        if conn is None:
            async with self.acquire() as local_conn:
                return await self.get_redemption(guild_id, redemption_id, conn=local_conn, for_update=for_update)
        clause = " FOR UPDATE" if for_update else ""
        row = await conn.fetchrow(
            f"SELECT * FROM reward_redemptions WHERE guild_id = $1 AND id = $2{clause}",
            guild_id,
            redemption_id,
        )
        return dict(row) if row else None

    async def update_redemption(self, guild_id: int, redemption_id: int, *, conn: asyncpg.Connection | None = None, **changes: Any) -> dict | None:
        if conn is None:
            async with self.acquire() as local_conn:
                return await self.update_redemption(guild_id, redemption_id, conn=local_conn, **changes)
        if not changes:
            return await self.get_redemption(guild_id, redemption_id, conn=conn)
        sets = [f"{k} = ${idx}" for idx, k in enumerate(changes.keys(), start=3)]
        await conn.execute(
            f"UPDATE reward_redemptions SET {', '.join(sets)} WHERE guild_id = $1 AND id = $2",
            guild_id,
            redemption_id,
            *changes.values(),
        )
        return await self.get_redemption(guild_id, redemption_id, conn=conn)

    async def list_pending_redemptions(self, guild_id: int, *, limit: int = 20, offset: int = 0) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.*, i.name AS item_name
                FROM reward_redemptions r
                JOIN reward_store_items i ON i.id = r.store_item_id
                WHERE r.guild_id = $1 AND r.status = 'pending'
                ORDER BY r.created_at ASC
                OFFSET $2 LIMIT $3
                """,
                guild_id,
                offset,
                limit,
            )
            return [dict(r) for r in rows]

    async def list_user_redemptions(self, guild_id: int, user_id: int, *, limit: int = 20) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.*, i.name AS item_name
                FROM reward_redemptions r
                JOIN reward_store_items i ON i.id = r.store_item_id
                WHERE r.guild_id = $1 AND r.user_id = $2
                ORDER BY r.id DESC
                LIMIT $3
                """,
                guild_id,
                user_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def lookup_torn_thumbnail(self, *, torn_item_id: int | None = None, torn_item_name: str | None = None) -> str | None:
        async with self.acquire() as conn:
            if torn_item_id:
                url = await conn.fetchval(
                    "SELECT image_url FROM torn_items WHERE item_id = $1",
                    int(torn_item_id),
                )
                if url:
                    return str(url)
            if torn_item_name:
                row = await conn.fetchrow(
                    """
                    SELECT image_url
                    FROM torn_items
                    WHERE norm_name = LOWER($1)
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    str(torn_item_name).strip(),
                )
                if row and row.get("image_url"):
                    return str(row["image_url"])
        return None

    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM reward_redemptions WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            return {"reward_redemptions": int(str(result).split()[-1])}

    async def list_guild_user_ids(self, guild_id: int) -> set[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM reward_redemptions WHERE guild_id = $1", guild_id)
            return {int(r["user_id"]) for r in rows if int(r["user_id"] or 0) > 0}
