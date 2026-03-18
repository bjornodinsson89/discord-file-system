from __future__ import annotations

from datetime import datetime, timezone

import discord

from repositories.store import StoreRepository
from repositories.torn_items import TornItemLookupError, TornItemsRepository
from services.prize_token_service import PrizeTokenService


class StoreService:
    def __init__(self, repo: StoreRepository, token_service: PrizeTokenService, torn_items_repo: TornItemsRepository | None = None):
        self.repo = repo
        self.token_service = token_service
        self.torn_items_repo = torn_items_repo

    async def resolve_thumbnail(self, item: dict) -> str | None:
        if item.get("thumbnail_url"):
            return str(item["thumbnail_url"])
        return await self.repo.lookup_torn_thumbnail(
            torn_item_id=item.get("torn_item_id"), torn_item_name=item.get("torn_item_name")
        )

    async def _apply_torn_item_metadata(self, payload: dict, *, missing_note_prefix: str) -> str | None:
        item_name = str(payload.get("name") or "").strip()
        payload["name"] = item_name
        payload["category"] = str(payload.get("category") or "").strip()
        payload["fulfillment_type"] = str(payload.get("fulfillment_type") or "admin_manual").strip()

        if payload["category"] != "torn_item" or self.torn_items_repo is None:
            return None

        try:
            match = await self.torn_items_repo.resolve_store_item_match_by_name(item_name)
        except TornItemLookupError as exc:
            raise ValueError(str(exc)) from exc

        if match is not None:
            payload["torn_item_id"] = int(match["item_id"])
            payload["torn_item_name"] = str(match["name"])
            payload["thumbnail_url"] = match.get("image_url")
            payload["name"] = str(match["name"])
            return None

        payload["torn_item_id"] = None
        payload["torn_item_name"] = item_name
        payload["thumbnail_url"] = None
        return f"{missing_note_prefix} no Torn image match was found for '{item_name}'."

    async def create_store_item(self, **payload) -> tuple[dict, str | None]:
        admin_note = await self._apply_torn_item_metadata(payload, missing_note_prefix="Added item, but")
        item = await self.repo.create_item(**payload)
        return item, admin_note

    async def update_store_item(self, *, guild_id: int, item_id: int, **changes) -> tuple[dict | None, str | None]:
        current = await self.repo.get_item(guild_id, item_id)
        if not current:
            return None, None

        payload = dict(current)
        payload.update({k: v for k, v in changes.items() if v is not None})
        admin_note = await self._apply_torn_item_metadata(payload, missing_note_prefix="Updated item, but")

        allowed_changes = {
            "name": payload.get("name"),
            "description": payload.get("description"),
            "category": payload.get("category"),
            "token_cost": payload.get("token_cost"),
            "stock": payload.get("stock"),
            "fulfillment_type": payload.get("fulfillment_type"),
            "discord_role_id": payload.get("discord_role_id"),
            "torn_item_name": payload.get("torn_item_name"),
            "torn_item_id": payload.get("torn_item_id"),
            "thumbnail_url": payload.get("thumbnail_url"),
        }
        updated = await self.repo.update_item(guild_id, item_id, **allowed_changes)
        return updated, admin_note

    async def _refund_role_redemption_failure(
        self,
        *,
        guild_id: int,
        redemption_id: int,
        actor_user_id: int,
        reason: str,
    ) -> None:
        await self.refund_redemption(
            guild_id=guild_id,
            redemption_id=redemption_id,
            admin_user_id=actor_user_id,
            notes=reason,
        )

    async def redeem_item(
        self, *, guild: discord.Guild, user: discord.Member, item_id: int
    ) -> tuple[dict | None, str | None]:
        role_id_for_grant: int | None = None
        redemption: dict | None = None

        async with self.repo.acquire() as conn:
            async with conn.transaction():
                settings = await self.repo.upsert_guild_settings_with_conn(conn, guild.id)
                if not bool(settings.get("enabled", False)):
                    return None, "Store is currently disabled."

                item = await self.repo.get_item(guild.id, item_id, for_update=True, conn=conn)
                if not item or not bool(item.get("is_active")):
                    return None, "That store item is not available."

                if item.get("category") == "torn_item" and not bool(
                    settings.get("torn_item_store_enabled", True)
                ):
                    return None, "Torn item store is currently disabled."
                if item.get("category") == "discord_perk" and not bool(
                    settings.get("discord_perk_store_enabled", True)
                ):
                    return None, "Discord perk store is currently disabled."

                stock = item.get("stock")
                if stock is not None and int(stock) <= 0:
                    return None, "That item is currently out of stock."

                max_per_user = item.get("max_per_user")
                if max_per_user is not None:
                    existing = await self.repo.count_user_redemptions_for_item(
                        guild.id, user.id, item_id, conn=conn
                    )
                    if existing >= int(max_per_user):
                        return None, "You reached the max redemptions for this item."

                fulfillment_type = str(item.get("fulfillment_type") or "admin_manual")
                if fulfillment_type == "discord_role":
                    role_id = int(item.get("discord_role_id") or 0)
                    role = guild.get_role(role_id) if role_id else None
                    if role is None:
                        return None, "Configured role for this perk no longer exists."
                    if role in user.roles:
                        return None, "You already have that role."
                    role_id_for_grant = role_id

                redemption = await self.repo.create_redemption(
                    conn=conn,
                    guild_id=guild.id,
                    user_id=user.id,
                    store_item_id=item_id,
                    token_cost=int(item.get("token_cost") or 0),
                    quantity=1,
                    status="pending",
                    fulfillment_type=fulfillment_type,
                    fulfilled_by=None,
                    fulfilled_at=None,
                    notes=None,
                )

                spent = await self.token_service.spend_store_tokens(
                    guild_id=guild.id,
                    user_id=user.id,
                    amount=int(item.get("token_cost") or 0),
                    source_id=str(redemption["id"]),
                    dedupe_key=f"store_redeem:{guild.id}:{user.id}:{redemption['id']}",
                    metadata={"store_item_id": item_id},
                    conn=conn,
                )
                if not spent:
                    raise ValueError("token_dedupe_rejected")

                if stock is not None:
                    await self.repo.adjust_stock(guild.id, item_id, -1, conn=conn)

        if redemption is None:
            return None, "Failed to create redemption."

        if role_id_for_grant is None:
            return redemption, None

        role = guild.get_role(int(role_id_for_grant))
        if role is None:
            await self._refund_role_redemption_failure(
                guild_id=guild.id,
                redemption_id=int(redemption["id"]),
                actor_user_id=user.id,
                reason="Auto-refund: configured Discord role no longer exists",
            )
            return None, "Configured role for this perk no longer exists."

        if role in user.roles:
            await self._refund_role_redemption_failure(
                guild_id=guild.id,
                redemption_id=int(redemption["id"]),
                actor_user_id=user.id,
                reason="Auto-refund: member already had role before assignment",
            )
            return None, "You already have that role."

        try:
            await user.add_roles(role, reason=f"Store redemption item #{item_id}")
        except Exception:
            await self._refund_role_redemption_failure(
                guild_id=guild.id,
                redemption_id=int(redemption["id"]),
                actor_user_id=user.id,
                reason="Auto-refund: Discord role assignment failed",
            )
            return None, "Failed to grant role. No tokens were charged."

        updated, err = await self.fulfill_redemption(
            guild_id=guild.id,
            redemption_id=int(redemption["id"]),
            admin_user_id=user.id,
            notes="Role granted automatically",
        )
        if err:
            return (
                redemption,
                "Role granted, but fulfillment status update failed. Please contact an admin.",
            )
        return updated, None

    async def fulfill_redemption(
        self,
        *,
        guild_id: int,
        redemption_id: int,
        admin_user_id: int,
        notes: str | None = None,
    ) -> tuple[dict | None, str | None]:
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                redemption = await self.repo.get_redemption(
                    guild_id, redemption_id, conn=conn, for_update=True
                )
                if not redemption:
                    return None, "Redemption not found."
                if redemption.get("status") != "pending":
                    return None, "Redemption is not pending."
                updated = await self.repo.update_redemption(
                    guild_id,
                    redemption_id,
                    conn=conn,
                    status="fulfilled",
                    fulfilled_by=admin_user_id,
                    fulfilled_at=datetime.now(timezone.utc),
                    notes=notes,
                )
                return updated, None

    async def refund_redemption(
        self,
        *,
        guild_id: int,
        redemption_id: int,
        admin_user_id: int,
        notes: str | None = None,
    ) -> tuple[dict | None, str | None]:
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                redemption = await self.repo.get_redemption(
                    guild_id, redemption_id, conn=conn, for_update=True
                )
                if not redemption:
                    return None, "Redemption not found."
                if redemption.get("status") not in {"pending", "fulfilled"}:
                    return None, "Redemption cannot be refunded."

                item = await self.repo.get_item(
                    guild_id, int(redemption["store_item_id"]), conn=conn, for_update=True
                )
                if item and item.get("stock") is not None:
                    await self.repo.adjust_stock(guild_id, int(item["id"]), +1, conn=conn)

                refunded = await self.token_service.refund_store_tokens(
                    guild_id=guild_id,
                    user_id=int(redemption["user_id"]),
                    amount=int(redemption.get("token_cost") or 0),
                    source_id=str(redemption_id),
                    dedupe_key=f"store_refund:{guild_id}:{redemption_id}",
                    metadata={"store_item_id": redemption.get("store_item_id")},
                    conn=conn,
                )
                if not refunded:
                    raise ValueError("token_refund_dedupe_rejected")

                updated = await self.repo.update_redemption(
                    guild_id,
                    redemption_id,
                    conn=conn,
                    status="refunded",
                    fulfilled_by=admin_user_id,
                    fulfilled_at=datetime.now(timezone.utc),
                    notes=notes,
                )
                return updated, None

    async def post_admin_redemption_message(
        self, guild: discord.Guild, redemption: dict, item: dict
    ) -> str | None:
        settings = await self.repo.get_or_create_guild_settings(guild.id)
        channel_id = int(settings.get("fulfillment_channel_id") or 0)
        if not channel_id:
            return "Store fulfillment channel is not configured."
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return "Configured store fulfillment channel is invalid."
        try:
            message = await channel.send(
                f"🧾 Store Redemption #{redemption['id']}\n"
                f"User: <@{redemption['user_id']}>\n"
                f"Item: {item.get('name')}\n"
                f"Cost: {redemption.get('token_cost')} tokens\n"
                f"Created: <t:{int(redemption['created_at'].timestamp())}:F>"
            )
            await self.repo.update_redemption(
                guild.id,
                int(redemption["id"]),
                admin_message_channel_id=channel.id,
                admin_message_id=message.id,
            )
            return None
        except Exception:
            return "Failed to post redemption message to fulfillment channel."
