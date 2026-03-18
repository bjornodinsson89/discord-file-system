from __future__ import annotations

from datetime import datetime, timezone

import discord

from repositories.store import StoreRepository
from repositories.torn_items import TornItemLookupError, TornItemsRepository
from services.happy_jump_dollar_service import HappyJumpDollarService


class StoreService:
    def __init__(self, repo: StoreRepository, hjd_service: HappyJumpDollarService, torn_items_repo: TornItemsRepository | None = None, cog=None):
        self.repo = repo
        self.hjd_service = hjd_service
        self.torn_items_repo = torn_items_repo
        self.cog = cog

    async def resolve_thumbnail(self, item: dict) -> str | None:
        if item.get("thumbnail_url"):
            return str(item["thumbnail_url"])
        return await self.repo.lookup_torn_thumbnail(
            torn_item_id=item.get("torn_item_id"), torn_item_name=item.get("torn_item_name")
        )

    async def resolve_description(self, item: dict) -> str:
        description = str(item.get("description") or "").strip()
        if description:
            return description
        if item.get("category") == "torn_item":
            fallback = await self.repo.lookup_torn_description(
                torn_item_id=item.get("torn_item_id"),
                torn_item_name=item.get("torn_item_name") or item.get("name"),
            )
            if fallback:
                return fallback
        return "No description provided."

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
            if not str(payload.get("description") or "").strip() and match.get("description"):
                payload["description"] = str(match["description"]).strip()
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

                spent = await self.hjd_service.spend_store_hjd(
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
            return None, "Failed to grant role. No HJD were charged."

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

                refunded = await self.hjd_service.refund_store_hjd(
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
                f"Cost: {redemption.get('token_cost')} HJD\n"
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


    def _storefront_channel(self, guild: discord.Guild, channel_id: int | None):
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        if channel is None or not hasattr(channel, "send"):
            return None
        return channel

    async def build_storefront_item_embed(self, item: dict) -> discord.Embed:
        embed = discord.Embed(
            title=item["name"],
            description=await self.resolve_description(item),
            colour=discord.Colour.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Price", value=f"{item['token_cost']} HJD")
        embed.add_field(name="Stock", value="Unlimited" if item.get("stock") is None else str(item.get("stock")))
        embed.add_field(name="Category", value="Torn Items" if item.get("category") == "torn_item" else "Discord Perks")
        embed.add_field(name="Fulfillment", value=str(item.get("fulfillment_type") or "admin_manual").replace("_", " ").title(), inline=False)
        thumb = await self.resolve_thumbnail(item)
        if thumb:
            embed.set_thumbnail(url=thumb)
        return embed

    async def sync_storefront(self, guild: discord.Guild) -> dict[str, int | None]:
        settings = await self.repo.get_or_create_guild_settings(guild.id)
        channel_id = settings.get("store_channel_id")
        previous_channel = self._storefront_channel(guild, settings.get("storefront_channel_id"))
        channel = self._storefront_channel(guild, channel_id)
        if channel is None:
            return {"channel_id": None, "hub_message_id": None, "admin_message_id": None}

        async def _delete_previous_message(target_channel, message_id: int | None):
            if target_channel is None or not message_id or target_channel.id == channel.id:
                return
            try:
                message = await target_channel.fetch_message(int(message_id))
                await message.delete()
            except Exception:
                return

        await _delete_previous_message(previous_channel, settings.get("store_hub_message_id"))
        await _delete_previous_message(previous_channel, settings.get("store_admin_message_id"))

        async def _ensure_message(message_id: int | None, *, embed: discord.Embed, view=None):
            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(embed=embed, view=view)
                    return message
                except Exception:
                    pass
            return await channel.send(embed=embed, view=view)

        hub_message = await _ensure_message(
            settings.get("store_hub_message_id"),
            embed=discord.Embed(
                title="Happy Jump Dollar Store",
                description=(
                    "Spend Happy Jump Dollars (HJD) on premium server rewards. Browse **Torn Items** and **Discord Perks**, "
                    "then scroll below to view and redeem items directly from this storefront channel."
                ),
                colour=discord.Colour.gold(),
                timestamp=datetime.now(timezone.utc),
            ).add_field(
                name="How it works",
                value="Earn 100 HJD on every level-up, then spend HJD here on live rewards.",
                inline=False,
            ).add_field(name="Available rewards", value="• Torn Items\n• Discord Perks", inline=False).set_footer(
                text="Scroll below to browse the live storefront items."
            ),
            view=None,
        )
        admin_message = await _ensure_message(
            settings.get("store_admin_message_id"),
            embed=discord.Embed(
                title="Store Controls",
                colour=discord.Colour.dark_gold(),
                timestamp=datetime.now(timezone.utc),
            ),
            view=self.cog.build_admin_storefront_view() if hasattr(self, "cog") else None,
        )

        await self.repo.upsert_guild_settings(
            guild.id,
            storefront_channel_id=channel.id,
            store_hub_message_id=hub_message.id,
            store_admin_message_id=admin_message.id,
        )

        active_items = await self.repo.get_storefront_items(guild.id)
        active_ids = {int(item["id"]) for item in active_items}
        all_items = await self.repo.list_all_guild_items(guild.id)

        for item in all_items:
            msg_id = item.get("storefront_message_id")
            if int(item["id"]) not in active_ids:
                target_channel = channel if int(item.get("storefront_channel_id") or channel.id) == channel.id else self._storefront_channel(guild, item.get("storefront_channel_id"))
                if msg_id and target_channel is not None:
                    try:
                        old_message = await target_channel.fetch_message(int(msg_id))
                        await old_message.delete()
                    except Exception:
                        pass
                await self.repo.update_item(guild.id, int(item["id"]), storefront_channel_id=None, storefront_message_id=None)
                continue

            embed = await self.build_storefront_item_embed(item)
            if msg_id:
                try:
                    if int(item.get("storefront_channel_id") or channel.id) != channel.id:
                        previous_item_channel = self._storefront_channel(guild, item.get("storefront_channel_id"))
                        await _delete_previous_message(previous_item_channel, msg_id)
                        raise LookupError("storefront channel changed")
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.edit(embed=embed, view=self.cog.build_redeem_view(int(item["id"])))
                    await self.repo.update_item(guild.id, int(item["id"]), storefront_channel_id=channel.id)
                    continue
                except Exception:
                    pass
            msg = await channel.send(embed=embed, view=self.cog.build_redeem_view(int(item["id"])))
            await self.repo.update_item(guild.id, int(item["id"]), storefront_channel_id=channel.id, storefront_message_id=msg.id)

        return {"channel_id": channel.id, "hub_message_id": hub_message.id, "admin_message_id": admin_message.id}
