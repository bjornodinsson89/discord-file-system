from __future__ import annotations

from datetime import datetime, timezone

import discord

from repositories.store import StoreRepository
from services.prize_token_service import PrizeTokenService


class StoreService:
    def __init__(self, repo: StoreRepository, token_service: PrizeTokenService):
        self.repo = repo
        self.token_service = token_service

    async def resolve_thumbnail(self, item: dict) -> str | None:
        if item.get("thumbnail_url"):
            return str(item["thumbnail_url"])
        return await self.repo.lookup_torn_thumbnail(
            torn_item_id=item.get("torn_item_id"), torn_item_name=item.get("torn_item_name")
        )

    async def redeem_item(self, *, guild: discord.Guild, user: discord.Member, item_id: int) -> tuple[dict | None, str | None]:
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                settings = await self.repo.upsert_guild_settings_with_conn(conn, guild.id)
                if not bool(settings.get("enabled", False)):
                    return None, "Store is currently disabled."

                item = await self.repo.get_item(guild.id, item_id, for_update=True, conn=conn)
                if not item or not bool(item.get("is_active")):
                    return None, "That store item is not available."

                if item.get("category") == "torn_item" and not bool(settings.get("torn_item_store_enabled", True)):
                    return None, "Torn item store is currently disabled."
                if item.get("category") == "discord_perk" and not bool(settings.get("discord_perk_store_enabled", True)):
                    return None, "Discord perk store is currently disabled."

                stock = item.get("stock")
                if stock is not None and int(stock) <= 0:
                    return None, "That item is currently out of stock."

                max_per_user = item.get("max_per_user")
                if max_per_user is not None:
                    existing = await self.repo.count_user_redemptions_for_item(guild.id, user.id, item_id, conn=conn)
                    if existing >= int(max_per_user):
                        return None, "You reached the max redemptions for this item."

                fulfillment_type = str(item.get("fulfillment_type") or "admin_manual")
                status = "pending"
                fulfilled_by = None
                fulfilled_at = None
                notes = None

                if fulfillment_type == "discord_role":
                    role_id = int(item.get("discord_role_id") or 0)
                    role = guild.get_role(role_id) if role_id else None
                    if role is None:
                        return None, "Configured role for this perk no longer exists."
                    if role in user.roles:
                        return None, "You already have that role."
                    try:
                        await user.add_roles(role, reason=f"Store redemption item #{item_id}")
                    except Exception:
                        return None, "Failed to grant role. Please contact an admin."
                    status = "fulfilled"
                    fulfilled_by = user.id
                    fulfilled_at = datetime.now(timezone.utc)
                    notes = "Role granted automatically"

                redemption = await self.repo.create_redemption(
                    conn=conn,
                    guild_id=guild.id,
                    user_id=user.id,
                    store_item_id=item_id,
                    token_cost=int(item.get("token_cost") or 0),
                    quantity=1,
                    status=status,
                    fulfillment_type=fulfillment_type,
                    fulfilled_by=fulfilled_by,
                    fulfilled_at=fulfilled_at,
                    notes=notes,
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

                return redemption, None

    async def fulfill_redemption(self, *, guild_id: int, redemption_id: int, admin_user_id: int, notes: str | None = None) -> tuple[dict | None, str | None]:
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                redemption = await self.repo.get_redemption(guild_id, redemption_id, conn=conn, for_update=True)
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

    async def refund_redemption(self, *, guild_id: int, redemption_id: int, admin_user_id: int, notes: str | None = None) -> tuple[dict | None, str | None]:
        async with self.repo.acquire() as conn:
            async with conn.transaction():
                redemption = await self.repo.get_redemption(guild_id, redemption_id, conn=conn, for_update=True)
                if not redemption:
                    return None, "Redemption not found."
                if redemption.get("status") not in {"pending", "fulfilled"}:
                    return None, "Redemption cannot be refunded."

                item = await self.repo.get_item(guild_id, int(redemption["store_item_id"]), conn=conn, for_update=True)
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

    async def post_admin_redemption_message(self, guild: discord.Guild, redemption: dict, item: dict) -> str | None:
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
            return "Failed to post redemption to fulfillment channel."
