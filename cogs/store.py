from __future__ import annotations

from datetime import datetime
from typing import Any

import discord
from discord.ext import commands

from repositories.prize_tokens import PrizeTokensRepository
from repositories.store import StoreRepository
from repositories.torn_items import TornItemsRepository
from services.prize_token_service import PrizeTokenService
from services.store_service import StoreService
from utils.database import get_pool

STORE_HUB_CUSTOM_ID = "storefront:hub"
STORE_ADMIN_CUSTOM_ID = "storefront:admin"
STORE_ITEM_CUSTOM_ID = "storefront:item"


def _member_is_store_admin(member: discord.Member, guild: discord.Guild) -> bool:
    if member.id == guild.owner_id:
        return True
    perms = member.guild_permissions
    return bool(perms.administrator or perms.manage_guild)


async def deny_unauthorized(interaction: discord.Interaction) -> None:
    if interaction.response.is_done():
        await interaction.followup.send("Only server admins can use these store controls.", ephemeral=True)
    else:
        await interaction.response.send_message("Only server admins can use these store controls.", ephemeral=True)


class StoreBrowseView(discord.ui.View):
    def __init__(self, cog: "StoreCog", category: str | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.category = category

    @discord.ui.button(label="Browse Torn Items", style=discord.ButtonStyle.primary, custom_id=f"{STORE_HUB_CUSTOM_ID}:torn")
    async def torn_items(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.send_item_browser(interaction, "torn_item")

    @discord.ui.button(label="Browse Discord Perks", style=discord.ButtonStyle.primary, custom_id=f"{STORE_HUB_CUSTOM_ID}:perks")
    async def perks(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.send_item_browser(interaction, "discord_perk")

    @discord.ui.button(label="My Redemptions", style=discord.ButtonStyle.secondary, custom_id=f"{STORE_HUB_CUSTOM_ID}:mine")
    async def my_redemptions(self, interaction: discord.Interaction, _: discord.ui.Button):
        rows = await self.cog.store_repo.list_user_redemptions(interaction.guild_id, interaction.user.id, limit=10)
        body = "\n".join(
            f"#{r['id']} · {r['item_name']} · {r['status']} · {r['token_cost']} tokens" for r in rows
        ) or "No redemptions yet."
        embed = discord.Embed(title="My Redemptions", description=body, colour=discord.Colour.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ConfirmRedeemView(discord.ui.View):
    def __init__(self, cog: "StoreCog", item_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.item_id = item_id

    @discord.ui.button(label="Confirm Redeem", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild context required.", ephemeral=True)
            return
        redemption, error = await self.cog.store_service.redeem_item(guild=interaction.guild, user=interaction.user, item_id=self.item_id)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        item = await self.cog.store_repo.get_item(interaction.guild.id, self.item_id)
        warning = None
        if redemption and redemption.get("status") == "pending" and item:
            warning = await self.cog.store_service.post_admin_redemption_message(interaction.guild, redemption, item)
        await self.cog.sync_storefront(interaction.guild)
        await interaction.response.send_message(
            f"Redeemed successfully. Redemption #{redemption['id']}" + (f"\n⚠️ {warning}" if warning else ""),
            ephemeral=True,
        )


class RedeemButtonView(discord.ui.View):
    def __init__(self, cog: "StoreCog", item_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.item_id = item_id

    @discord.ui.button(label="Redeem", style=discord.ButtonStyle.success, custom_id=f"{STORE_ITEM_CUSTOM_ID}:redeem")
    async def redeem(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.send_item_detail(interaction, self.item_id)


class DiscordRoleConfigModal(discord.ui.Modal, title="Discord Role Details"):
    role_id = discord.ui.TextInput(label="Role ID", required=False, placeholder="Required for fulfillment type = discord_role")

    def __init__(self, parent: "AddStoreItemModal"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        await self.parent.finish_submit(interaction, fulfillment_type="discord_role", role_id_raw=str(self.role_id.value).strip() or None)


class FulfillmentTypeView(discord.ui.View):
    def __init__(self, modal: "AddStoreItemModal", user_id: int):
        super().__init__(timeout=180)
        self.modal = modal
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the admin who started this flow can use it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Admin Manual", style=discord.ButtonStyle.primary)
    async def admin_manual(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.modal.finish_submit(interaction, fulfillment_type="admin_manual")

    @discord.ui.button(label="Discord Role", style=discord.ButtonStyle.secondary)
    async def discord_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(DiscordRoleConfigModal(self.modal))


class AddStoreItemModal(discord.ui.Modal, title="Add Store Item"):
    name = discord.ui.TextInput(label="Name", max_length=100)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=500)
    category = discord.ui.TextInput(label="Category", default="torn_item")
    token_cost = discord.ui.TextInput(label="Token cost", default="1")
    stock = discord.ui.TextInput(label="Stock", required=False, placeholder="Leave blank for unlimited")

    def __init__(self, cog: "StoreCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        category = str(self.category.value).strip().lower()
        if category == "discord_perk":
            await interaction.response.send_message(
                "Choose a fulfillment type for this Discord perk.",
                ephemeral=True,
                view=FulfillmentTypeView(self, interaction.user.id),
            )
            return
        await self.finish_submit(interaction, fulfillment_type="admin_manual")

    async def finish_submit(self, interaction: discord.Interaction, *, fulfillment_type: str, role_id_raw: str | None = None):
        category = str(self.category.value).strip().lower()
        stock_raw = str(self.stock.value).strip()
        if fulfillment_type == "discord_role" and not role_id_raw:
            await interaction.response.send_message("Role ID is required for Discord role fulfillment.", ephemeral=True)
            return
        try:
            item, admin_note = await self.cog.store_service.create_store_item(
                guild_id=interaction.guild_id,
                name=str(self.name.value).strip(),
                description=str(self.description.value).strip() or None,
                category=category,
                token_cost=int(str(self.token_cost.value).strip()),
                stock=int(stock_raw) if stock_raw else None,
                fulfillment_type=fulfillment_type,
                discord_role_id=int(role_id_raw) if role_id_raw else None,
                created_by=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.sync_storefront(interaction.guild)
        message = f"Added item #{item['id']}."
        if admin_note:
            message = f"{message}\n⚠️ {admin_note}"
        elif category == "torn_item" and item.get("thumbnail_url"):
            message = f"{message} Thumbnail resolved automatically from Torn item data."
        await interaction.response.send_message(message, ephemeral=True)


class UpdateItemModal(discord.ui.Modal, title="Edit Store Item"):
    item_id = discord.ui.TextInput(label="Store Item ID")
    name = discord.ui.TextInput(label="New name (optional)", required=False, max_length=100)
    token_cost = discord.ui.TextInput(label="New token cost")
    stock = discord.ui.TextInput(label="New stock (blank = unlimited)", required=False)

    def __init__(self, cog: "StoreCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        iid = int(str(self.item_id.value).strip())
        stock_raw = str(self.stock.value).strip()
        name_raw = str(self.name.value).strip()
        try:
            updated, admin_note = await self.cog.store_service.update_store_item(
                guild_id=interaction.guild_id,
                item_id=iid,
                name=name_raw or None,
                token_cost=int(str(self.token_cost.value).strip()),
                stock=int(stock_raw) if stock_raw else None,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not updated:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        await self.cog.sync_storefront(interaction.guild)
        message = f"Updated item #{updated['id']}."
        if admin_note:
            message = f"{message}\n⚠️ {admin_note}"
        elif updated.get("category") == "torn_item" and name_raw:
            message = f"{message} Torn item metadata refreshed from the entered name."
        await interaction.response.send_message(message, ephemeral=True)


class StockAdjustModal(discord.ui.Modal):
    item_id = discord.ui.TextInput(label="Item ID")
    amount = discord.ui.TextInput(label="Amount", required=False)

    def __init__(self, cog: "StoreCog", title: str, *, disable: bool = False):
        super().__init__(title=title)
        self.cog = cog
        self.disable = disable

    async def on_submit(self, interaction: discord.Interaction):
        item_id = int(str(self.item_id.value).strip())
        if self.disable:
            row = await self.cog.store_repo.update_item(interaction.guild_id, item_id, is_active=False)
            await self.cog.sync_storefront(interaction.guild)
            await interaction.response.send_message("Item disabled." if row else "Item not found.", ephemeral=True)
            return
        row = await self.cog.store_repo.adjust_stock(interaction.guild_id, item_id, int(str(self.amount.value).strip() or "0"))
        await self.cog.sync_storefront(interaction.guild)
        await interaction.response.send_message("Stock updated." if row else "Item not found.", ephemeral=True)


class RedemptionActionModal(discord.ui.Modal):
    redemption_id = discord.ui.TextInput(label="Redemption ID")
    notes = discord.ui.TextInput(label="Notes", style=discord.TextStyle.paragraph, required=False)

    def __init__(self, cog: "StoreCog", *, action: str):
        super().__init__(title=f"{action.title()} Redemption")
        self.cog = cog
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        rid = int(str(self.redemption_id.value).strip())
        if self.action == "fulfill":
            updated, err = await self.cog.store_service.fulfill_redemption(
                guild_id=interaction.guild_id,
                redemption_id=rid,
                admin_user_id=interaction.user.id,
                notes=str(self.notes.value).strip() or None,
            )
        else:
            updated, err = await self.cog.store_service.refund_redemption(
                guild_id=interaction.guild_id,
                redemption_id=rid,
                admin_user_id=interaction.user.id,
                notes=str(self.notes.value).strip() or None,
            )
        await self.cog.sync_storefront(interaction.guild)
        await interaction.response.send_message(err or f"Redemption #{updated['id']} marked {updated['status']}.", ephemeral=True)


class AdminStorefrontView(discord.ui.View):
    def __init__(self, cog: "StoreCog"):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await deny_unauthorized(interaction)
            return False
        allowed = _member_is_store_admin(member, guild)
        if not allowed:
            await deny_unauthorized(interaction)
        return allowed

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.success, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:add")
    async def add_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddStoreItemModal(self.cog))

    @discord.ui.button(label="Edit Item", style=discord.ButtonStyle.secondary, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:edit")
    async def edit_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(UpdateItemModal(self.cog))

    @discord.ui.button(label="Restock Item", style=discord.ButtonStyle.secondary, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:restock")
    async def restock_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockAdjustModal(self.cog, title="Restock Item"))

    @discord.ui.button(label="Disable Item", style=discord.ButtonStyle.danger, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:disable")
    async def disable_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(StockAdjustModal(self.cog, title="Disable Item", disable=True))

    @discord.ui.button(label="View Pending Redemptions", style=discord.ButtonStyle.primary, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:pending", row=1)
    async def pending_queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        rows = await self.cog.store_repo.list_pending_redemptions(interaction.guild_id, limit=10)
        body = "\n".join(f"#{r['id']} · <@{r['user_id']}> · {r['item_name']} · {r['status']}" for r in rows) or "No pending redemptions."
        await interaction.response.send_message(embed=discord.Embed(title="Pending Redemptions", description=body), ephemeral=True)

    @discord.ui.button(label="Fulfill Redemption", style=discord.ButtonStyle.success, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:fulfill", row=1)
    async def fulfill(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RedemptionActionModal(self.cog, action="fulfill"))

    @discord.ui.button(label="Refund Redemption", style=discord.ButtonStyle.danger, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:refund", row=1)
    async def refund(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RedemptionActionModal(self.cog, action="refund"))

    @discord.ui.button(label="Refresh Storefront", style=discord.ButtonStyle.primary, custom_id=f"{STORE_ADMIN_CUSTOM_ID}:refresh", row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.sync_storefront(interaction.guild)
        await interaction.response.send_message("Storefront refreshed.", ephemeral=True)


class StoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        pool = get_pool()
        self.store_repo = StoreRepository(pool)
        self.torn_items_repo = TornItemsRepository(pool)
        self.store_service = StoreService(
            self.store_repo,
            PrizeTokenService(PrizeTokensRepository(pool)),
            self.torn_items_repo,
            cog=self,
        )
        self.bot.add_view(StoreBrowseView(self))
        self.bot.add_view(AdminStorefrontView(self))


    def build_store_browse_view(self) -> StoreBrowseView:
        return StoreBrowseView(self)

    def build_admin_storefront_view(self) -> AdminStorefrontView:
        return AdminStorefrontView(self)

    def build_redeem_view(self, item_id: int) -> RedeemButtonView:
        return RedeemButtonView(self, item_id)

    def build_store_hub_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Prize Token Store",
            description=(
                "Spend Prize Tokens on premium server rewards. Browse **Torn Items** and **Discord Perks**, "
                "then redeem directly from this storefront channel."
            ),
            colour=discord.Colour.gold(),
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="How it works", value="Earn Prize Tokens through the community, then spend them here on live rewards.", inline=False)
        embed.add_field(name="Available rewards", value="• Torn Items\n• Discord Perks", inline=False)
        embed.set_footer(text="Use the buttons below to browse the storefront or review your redemption history.")
        return embed

    def build_admin_controls_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Store Admin Controls",
            description="Admins can manage the live storefront from here. Non-admin button presses are denied privately.",
            colour=discord.Colour.dark_gold(),
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Inventory", value="Add, edit, restock, disable, and refresh storefront items.", inline=False)
        embed.add_field(name="Redemptions", value="View pending redemptions, fulfill rewards, or issue refunds.", inline=False)
        return embed

    async def send_item_browser(self, interaction: discord.Interaction, category: str):
        items = await self.store_repo.list_items(interaction.guild_id, category=category, active_only=True)
        category_label = "Torn Items" if category == "torn_item" else "Discord Perks"
        desc = "\n".join(
            f"`#{i['id']}` **{i['name']}** · {i['token_cost']} tokens · stock {i.get('stock', '∞')}" for i in items[:15]
        ) or "No items available."
        await interaction.response.send_message(
            embed=discord.Embed(title=category_label, description=desc, colour=discord.Colour.blurple()),
            ephemeral=True,
        )

    async def build_item_embed(self, item: dict) -> discord.Embed:
        embed = discord.Embed(
            title=item["name"],
            description=item.get("description") or "No description provided.",
            colour=discord.Colour.green(),
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Token cost", value=str(item["token_cost"]))
        embed.add_field(name="Stock", value="Unlimited" if item.get("stock") is None else str(item.get("stock")))
        embed.add_field(name="Category", value="Torn Items" if item.get("category") == "torn_item" else "Discord Perks")
        embed.add_field(name="Fulfillment", value=str(item.get("fulfillment_type") or "admin_manual").replace("_", " ").title(), inline=False)
        thumb = await self.store_service.resolve_thumbnail(item)
        if thumb:
            embed.set_thumbnail(url=thumb)
        return embed

    async def send_item_detail(self, interaction: discord.Interaction, item_id: int):
        item = await self.store_repo.get_item(interaction.guild_id, item_id)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        embed = await self.build_item_embed(item)
        await interaction.response.send_message(embed=embed, view=ConfirmRedeemView(self, item_id), ephemeral=True)

    async def sync_storefront(self, guild: discord.Guild | None) -> None:
        if guild is None:
            return
        await self.store_service.sync_storefront(guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(StoreCog(bot))
