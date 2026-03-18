from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from repositories.prize_tokens import PrizeTokensRepository
from repositories.store import StoreRepository
from services.prize_token_service import PrizeTokenService
from services.store_service import StoreService
from utils.database import get_pool


class StoreHomeView(discord.ui.View):
    def __init__(self, cog: "StoreCog", user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the command user can use this panel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Torn Items", style=discord.ButtonStyle.primary)
    async def torn_items(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await self.cog.send_item_browser(interaction, "torn_item")

    @discord.ui.button(label="Discord Perks", style=discord.ButtonStyle.primary)
    async def perks(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await self.cog.send_item_browser(interaction, "discord_perk")

    @discord.ui.button(label="My Redemptions", style=discord.ButtonStyle.secondary)
    async def my_redemptions(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        rows = await self.cog.store_repo.list_user_redemptions(interaction.guild_id, interaction.user.id, limit=10)
        body = "\n".join(f"#{r['id']} · {r['item_name']} · {r['status']} · {r['token_cost']} tokens" for r in rows) or "No redemptions yet."
        await interaction.response.edit_message(embed=discord.Embed(title="My Redemptions", description=body), view=self)


class ItemSelect(discord.ui.Select):
    def __init__(self, cog: "StoreCog", items: list[dict]):
        super().__init__(
            placeholder="Choose an item",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=i["name"][:100], value=str(i["id"]), description=f"{i['token_cost']} tokens")
                for i in items[:25]
            ],
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.send_item_detail(interaction, int(self.values[0]))


class ItemBrowserView(discord.ui.View):
    def __init__(self, cog: "StoreCog", items: list[dict]):
        super().__init__(timeout=300)
        if items:
            self.add_item(ItemSelect(cog, items))


class ConfirmRedeemView(discord.ui.View):
    def __init__(self, cog: "StoreCog", user_id: int, item_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.item_id = item_id

    @discord.ui.button(label="Confirm Redeem", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the command user can redeem this.", ephemeral=True)
            return
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
        await interaction.response.send_message(
            f"Redeemed successfully. Redemption #{redemption['id']}" + (f"\n⚠️ {warning}" if warning else ""),
            ephemeral=True,
        )


class AddStoreItemModal(discord.ui.Modal, title="Add Store Item"):
    name = discord.ui.TextInput(label="Name", max_length=100)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=500)
    category = discord.ui.TextInput(label="Category", default="torn_item")
    token_cost = discord.ui.TextInput(label="Token cost", default="1")
    extras = discord.ui.TextInput(
        label="Extras (role_id,stock,torn_item_name)",
        required=False,
        placeholder="role_id=123;stock=5;torn_item_name=Xanax",
    )

    def __init__(self, cog: "StoreCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        extras = {}
        for part in str(self.extras.value).split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            extras[k.strip()] = v.strip()
        item = await self.cog.store_repo.create_item(
            guild_id=interaction.guild_id,
            name=str(self.name.value).strip(),
            description=str(self.description.value).strip() or None,
            category=str(self.category.value).strip(),
            token_cost=int(str(self.token_cost.value).strip()),
            stock=int(extras["stock"]) if extras.get("stock") else None,
            fulfillment_type=extras.get("fulfillment_type") or "admin_manual",
            discord_role_id=int(extras["role_id"]) if extras.get("role_id") else None,
            torn_item_name=extras.get("torn_item_name") or None,
            max_per_user=int(extras["max_per_user"]) if extras.get("max_per_user") else None,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(f"Added item #{item['id']}.", ephemeral=True)


class UpdateItemModal(discord.ui.Modal, title="Update Item"):
    item_id = discord.ui.TextInput(label="Item ID")
    token_cost = discord.ui.TextInput(label="New token cost")
    stock = discord.ui.TextInput(label="New stock (blank = unlimited)", required=False)

    def __init__(self, cog: "StoreCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        iid = int(str(self.item_id.value).strip())
        stock_raw = str(self.stock.value).strip()
        updated = await self.cog.store_repo.update_item(
            interaction.guild_id,
            iid,
            token_cost=int(str(self.token_cost.value).strip()),
            stock=int(stock_raw) if stock_raw else None,
        )
        await interaction.response.send_message(f"Updated item #{updated['id']}." if updated else "Item not found.", ephemeral=True)


class StockAdjustModal(discord.ui.Modal):
    item_id = discord.ui.TextInput(label="Item ID")
    amount = discord.ui.TextInput(label="Amount")

    def __init__(self, cog: "StoreCog", title: str, *, disable: bool = False):
        super().__init__(title=title)
        self.cog = cog
        self.disable = disable

    async def on_submit(self, interaction: discord.Interaction):
        item_id = int(str(self.item_id.value).strip())
        if self.disable:
            row = await self.cog.store_repo.update_item(interaction.guild_id, item_id, is_active=False)
            await interaction.response.send_message("Item disabled." if row else "Item not found.", ephemeral=True)
            return
        row = await self.cog.store_repo.adjust_stock(interaction.guild_id, item_id, int(str(self.amount.value).strip()))
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
        await interaction.response.send_message(err or f"Redemption #{updated['id']} marked {updated['status']}.", ephemeral=True)


class AdminStoreView(discord.ui.View):
    def __init__(self, cog: "StoreCog", user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id and isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild

    @discord.ui.button(label="Add Item", style=discord.ButtonStyle.primary, row=0)
    async def add_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
            return
        await interaction.response.send_modal(AddStoreItemModal(self.cog))

    @discord.ui.button(label="Edit Item", style=discord.ButtonStyle.primary, row=0)
    async def edit_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await interaction.response.send_modal(UpdateItemModal(self.cog))

    @discord.ui.button(label="Restock Item", style=discord.ButtonStyle.primary, row=1)
    async def restock_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await interaction.response.send_modal(StockAdjustModal(self.cog, "Restock Item"))

    @discord.ui.button(label="Disable Item", style=discord.ButtonStyle.secondary, row=1)
    async def disable_item(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await interaction.response.send_modal(StockAdjustModal(self.cog, "Disable Item", disable=True))

    @discord.ui.button(label="View Pending Redemptions", style=discord.ButtonStyle.primary, row=2)
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        rows = await self.cog.store_repo.list_pending_redemptions(interaction.guild_id, limit=10)
        desc = "\n".join(f"#{r['id']} · {r['item_name']} · <@{r['user_id']}> · {r['token_cost']}" for r in rows) or "No pending redemptions."
        await interaction.response.send_message(embed=discord.Embed(title="Pending Redemptions", description=desc), ephemeral=True)

    @discord.ui.button(label="Fulfill Redemption", style=discord.ButtonStyle.success, row=3)
    async def fulfill(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await interaction.response.send_modal(RedemptionActionModal(self.cog, action="fulfill"))

    @discord.ui.button(label="Refund Redemption", style=discord.ButtonStyle.danger, row=3)
    async def refund(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await interaction.response.send_modal(RedemptionActionModal(self.cog, action="refund"))


class StoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        pool = get_pool()
        self.store_repo = StoreRepository(pool)
        self.store_service = StoreService(self.store_repo, PrizeTokenService(PrizeTokensRepository(pool)))

    async def send_item_browser(self, interaction: discord.Interaction, category: str):
        items = await self.store_repo.list_items(interaction.guild_id, category=category, active_only=True)
        desc = "\n".join(f"`#{i['id']}` **{i['name']}** · {i['token_cost']} tokens · stock {i.get('stock', '∞')}" for i in items[:10]) or "No items available."
        await interaction.response.edit_message(embed=discord.Embed(title="Store Browser", description=desc), view=ItemBrowserView(self, items))

    async def send_item_detail(self, interaction: discord.Interaction, item_id: int):
        item = await self.store_repo.get_item(interaction.guild_id, item_id)
        if not item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        embed = discord.Embed(title=item["name"], description=item.get("description") or "No description", timestamp=datetime.utcnow())
        embed.add_field(name="Token cost", value=str(item["token_cost"]))
        embed.add_field(name="Stock", value=str(item.get("stock", "∞")))
        embed.add_field(name="Fulfillment", value=str(item.get("fulfillment_type")))
        thumb = await self.store_service.resolve_thumbnail(item)
        if thumb:
            embed.set_thumbnail(url=thumb)
        await interaction.response.send_message(embed=embed, view=ConfirmRedeemView(self, interaction.user.id, item_id), ephemeral=True)

    @app_commands.command(name="store", description="Open the Prize Token store")
    async def store(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(title="Prize Token Store", description="Browse Torn items, Discord perks, and your redemptions."),
            view=StoreHomeView(self, interaction.user.id),
            ephemeral=True,
        )



async def setup(bot: commands.Bot):
    await bot.add_cog(StoreCog(bot))
