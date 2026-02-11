"""
Raffle system with sell-out trigger support and automatic payment verification.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.raffles import RafflesRepository
from services.raffle_payment import RafflePaymentService
from utils import GuildSettingsRepository
from utils.database import get_database, get_pool

log = logging.getLogger("happy_jumper.raffles")


class RaffleCreateModal(discord.ui.Modal):
    """Modal for creating a new raffle."""

    prize = discord.ui.TextInput(
        label="🎁 Prize",
        placeholder="What are you giving away?",
        required=True,
        max_length=200
    )

    payment_type = discord.ui.TextInput(
        label="💰 Payment Type",
        placeholder="free | xanax | erotic_dvd",
        required=True,
        max_length=20,
        default="xanax"
    )

    ticket_price = discord.ui.TextInput(
        label="💵 Ticket Price",
        placeholder="Integer (ignored for free)",
        required=True,
        max_length=6,
        default="1"
    )

    tickets_available = discord.ui.TextInput(
        label="🎟️ Total Tickets",
        placeholder="Total tickets to sell (minimum 1)",
        required=True,
        max_length=10
    )

    max_per_user = discord.ui.TextInput(
        label="📋 Max Per User (0 = unlimited)",
        placeholder="0 = unlimited",
        required=True,
        max_length=3,
        default="0"
    )

    def __init__(self):
        super().__init__(title="🎉 Create Raffle")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            payment_type = (self.payment_type.value or "").strip().lower()

            if payment_type not in {"free", "xanax", "erotic_dvd"}:
                await interaction.response.send_message(
                    "❌ Payment Type must be one of: free, xanax, erotic_dvd",
                    ephemeral=True
                )
                return

            price = int(self.ticket_price.value) if self.ticket_price.value else 0
            total = int(self.tickets_available.value)
            max_per = int(self.max_per_user.value or 0)

            if price < 0:
                await interaction.response.send_message(
                    "❌ Ticket Price must be 0 or greater", ephemeral=True
                )
                return

            if total < 1:
                await interaction.response.send_message(
                    "❌ Total Tickets must be 1 or greater", ephemeral=True
                )
                return

            if max_per < 0:
                await interaction.response.send_message(
                    "❌ Max Tickets Per User must be 0 or greater", ephemeral=True
                )
                return

        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid numbers provided", ephemeral=True
            )
            return

        end_time = datetime.utcnow() + timedelta(days=30)
        end_trigger = "tickets_sold"
        hours_after_sold_out = None

        # Force price to 0 for free entries
        actual_price = 0 if payment_type == "free" else price

        if payment_type != "free" and actual_price <= 0:
            await interaction.response.send_message(
                "❌ Ticket Price must be greater than 0 unless Payment Type is free",
                ephemeral=True,
            )
            return

        if payment_type != "free":
            db = get_database()
            creator_key = await db.get_user_api_key(interaction.user.id)
            if not creator_key or not creator_key.get("torn_user_id"):
                await interaction.response.send_message(
                    "❌ You must link your Torn API key first to create paid raffles.",
                    ephemeral=True,
                )
                return

        repo = RafflesRepository(get_pool())

        try:
            raffle_id = await repo.create_raffle(
                guild_id=interaction.guild_id,
                creator_discord_id=interaction.user.id,
                prize=self.prize.value,
                ticket_payment_type=payment_type,
                ticket_price=actual_price,
                tickets_available=total,
                max_tickets_per_user=max_per,
                end_time=end_time,
                end_trigger=end_trigger,
                hours_after_sold_out=hours_after_sold_out
            )

            # Build display with emojis
            if payment_type == "free":
                price_display = "🎫 FREE"
            elif payment_type == "xanax":
                price_display = f"💊 {actual_price} Xanax"
            else:
                price_display = f"📀 {actual_price} Erotic DVD"

            embed = discord.Embed(
                title="🎉 New Raffle Created!",
                description=f"🎁 **Prize:** {self.prize.value}\n"
                           f"🎟️ **Tickets:** {total} available\n"
                           f"💰 **Price:** {price_display} per ticket\n"
                           f"📋 **Max per user:** {'Unlimited ♾️' if max_per == 0 else max_per}\n"
                           "⏰ **Draw occurs 30 seconds after sellout.**",
                color=discord.Color.green()
            )

            if payment_type == "free":
                embed.add_field(
                    name="✨ Free Entry",
                    value="🎫 No payment required! Click the button on the raffle card to enter.",
                    inline=False
                )

            db = get_database()
            settings_repo = GuildSettingsRepository(db)
            settings = await settings_repo.get_or_create(interaction.guild_id)

            purchase_channel_id = settings.get("raffle_purchase_channel_id") or settings.get("raffle_channel_id")
            if not purchase_channel_id:
                await interaction.response.send_message(
                    "❌ Configure **raffle purchase panel channel** in `/setup` before creating raffles.",
                    ephemeral=True,
                )
                return

            guild = interaction.guild
            purchase_channel = guild.get_channel(int(purchase_channel_id)) if guild else None
            if purchase_channel is None and guild:
                try:
                    fetched = await guild.fetch_channel(int(purchase_channel_id))
                    if hasattr(fetched, "send"):
                        purchase_channel = fetched
                except Exception:
                    purchase_channel = None

            if purchase_channel is None:
                await interaction.response.send_message(
                    "❌ Raffle purchase panel channel is invalid or inaccessible. Update it in `/setup`.",
                    ephemeral=True,
                )
                return

            purchase_panel_embed = discord.Embed(
                title=f"🎟️ Raffle #{raffle_id}: {self.prize.value}",
                description="Use the buttons below to buy tickets or check your entry.\n"
                            "⏰ **Draw occurs 30 seconds after sellout.**",
                color=discord.Color.blurple(),
            )
            purchase_panel_embed.add_field(name="Price", value=price_display, inline=True)
            purchase_panel_embed.add_field(name="Tickets", value=f"{total}", inline=True)
            purchase_panel_embed.add_field(
                name="Max per user",
                value="Unlimited ♾️" if max_per == 0 else str(max_per),
                inline=True,
            )

            panel_message = await purchase_channel.send(
                embed=purchase_panel_embed,
                view=RafflePurchasePanelView(raffle_id=raffle_id),
            )

            if bool(settings.get("raffle_announce_enabled", True)):
                announce_channel_id = settings.get("raffle_announcement_channel_id")
                if announce_channel_id:
                    announce_channel = guild.get_channel(int(announce_channel_id)) if guild else None
                    if announce_channel is None and guild:
                        try:
                            fetched = await guild.fetch_channel(int(announce_channel_id))
                            if hasattr(fetched, "send"):
                                announce_channel = fetched
                        except Exception:
                            announce_channel = None
                    if announce_channel is not None:
                        await announce_channel.send(embed=embed)

            await interaction.response.send_message(
                f"✅ Raffle created. Purchase panel posted in {purchase_channel.mention}.\n{panel_message.jump_url}",
                ephemeral=True,
            )

        except Exception as e:
            log.error(f"Failed to create raffle: {e}")
            await interaction.response.send_message(
                "❌ Failed to create raffle", ephemeral=True
            )


class RaffleBuyModal(discord.ui.Modal):
    """Modal for buying raffle tickets."""

    quantity = discord.ui.TextInput(
        label="🎟️ Number of Tickets",
        placeholder="How many tickets?",
        required=True,
        max_length=10
    )

    def __init__(self, raffle_id: int, repo: RafflesRepository):
        super().__init__(title="🎫 Buy Raffle Tickets")
        self.raffle_id = raffle_id
        self.repo = repo

    async def on_submit(self, interaction: discord.Interaction):
        try:
            quantity = int(self.quantity.value)
            if quantity < 1:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid quantity", ephemeral=True
            )
            return

        # Check raffle
        raffle = await self.repo.get_raffle(self.raffle_id)
        if not raffle:
            await interaction.response.send_message(
                "❌ Raffle not found", ephemeral=True
            )
            return

        if raffle["status"] != "active":
            await interaction.response.send_message(
                "❌ This raffle is no longer active", ephemeral=True
            )
            return

        # Check if sold out
        if raffle["tickets_sold"] >= raffle["tickets_available"]:
            await interaction.response.send_message(
                "❌ Sold out!", ephemeral=True
            )
            return

        # Check max per user
        if raffle["max_tickets_per_user"] > 0:
            entries = await self.repo.get_raffle_entries(self.raffle_id)
            user_tickets = sum(
                e["num_tickets"] for e in entries
                if e["discord_id"] == interaction.user.id and e["payment_verified"]
            )
            if user_tickets + quantity > raffle["max_tickets_per_user"]:
                await interaction.response.send_message(
                    f"❌ Max {raffle['max_tickets_per_user']} tickets per user. "
                    f"You have {user_tickets}.", ephemeral=True
                )
                return

        # Check availability
        if raffle["tickets_sold"] + quantity > raffle["tickets_available"]:
            remaining = raffle["tickets_available"] - raffle["tickets_sold"]
            await interaction.response.send_message(
                f"❌ Only {remaining} tickets left!", ephemeral=True
            )
            return

        # Handle FREE ENTRY
        if raffle["ticket_payment_type"] == "free":
            try:
                entry = await self.repo.reserve_free_entry(
                    raffle_id=self.raffle_id,
                    discord_id=interaction.user.id,
                    torn_user_id=0,
                    num_tickets=quantity
                )
                
                if not entry:
                    await interaction.response.send_message(
                        "❌ Failed to enter raffle", ephemeral=True
                    )
                    return
                
                embed = discord.Embed(
                    title="✅ Entry Confirmed!",
                    description=f"🎁 **Raffle:** {raffle['prize']}\n"
                               f"🎟️ **Tickets:** {quantity}\n"
                               f"💰 **Price:** 🎫 FREE",
                    color=discord.Color.green()
                )
                
                # Check if sold out
                updated_raffle = await self.repo.get_raffle(self.raffle_id)
                if updated_raffle["tickets_fully_sold_at"]:
                    embed.add_field(
                        name="🎉 SOLD OUT!",
                        value="This raffle is now full! Drawing soon.",
                        inline=False
                    )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
                
            except Exception as e:
                log.error(f"Failed free entry: {e}")
                await interaction.response.send_message(
                    "❌ Failed to enter raffle", ephemeral=True
                )
                return

        # PAID ENTRY
        db = get_database()
        buyer_key = await db.get_user_api_key(interaction.user.id)
        if not buyer_key or not buyer_key.get("torn_user_id"):
            await interaction.response.send_message(
                "❌ You must link your Torn API key first to buy paid raffle tickets.",
                ephemeral=True,
            )
            return

        creator_torn_id = raffle.get("creator_torn_id")
        if not creator_torn_id:
            creator_key = await db.get_user_api_key(int(raffle["creator_discord_id"]))
            creator_torn_id = creator_key.get("torn_user_id") if creator_key else None
        if not creator_torn_id:
            await interaction.response.send_message(
                "❌ Raffle creator Torn ID is not configured. Please contact an admin.",
                ephemeral=True,
            )
            return

        reserved_until = datetime.utcnow() + timedelta(minutes=5)

        try:
            entry = await self.repo.reserve_entry(
                raffle_id=self.raffle_id,
                discord_id=interaction.user.id,
                torn_user_id=int(buyer_key["torn_user_id"]),
                num_tickets=quantity,
                reserved_until=reserved_until
            )

            if not entry:
                await interaction.response.send_message(
                    "❌ Failed to reserve tickets", ephemeral=True
                )
                return

            total_cost = quantity * raffle["ticket_price"]
            emoji = "💊" if raffle["ticket_payment_type"] == "xanax" else "📀"

            embed = discord.Embed(
                title="🎫 Tickets Reserved!",
                description=f"🎁 **Raffle:** {raffle['prize']}\n"
                           f"🎟️ **Tickets:** {quantity}\n"
                           f"💰 **Total:** {emoji} {total_cost} {raffle['ticket_payment_type']}",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="⏰ Payment Deadline",
                value=f"⏱️ Auto-verification at 4:30, expires at 5:00\nSend **{total_cost} {raffle['ticket_payment_type']}** to raffle creator in-game!",
                inline=False
            )
            embed.add_field(
                name="💳 How to Pay",
                value="📨 Send items via Torn, bot will auto-detect. Click '✅ Verify Now' to check early.",
                inline=False
            )

            view = PaymentVerificationView(self.raffle_id, entry["entry_id"], self.repo, manual=True)

            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )

        except Exception as e:
            log.error(f"Failed to reserve entry: {e}")
            await interaction.response.send_message(
                "❌ Failed to reserve tickets", ephemeral=True
            )


class RafflePurchasePanelView(discord.ui.View):
    """Persistent purchase panel for raffle interactions."""

    def __init__(self, raffle_id: int):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id

    @discord.ui.button(label="🎟️ Buy Tickets", style=discord.ButtonStyle.success)
    async def buy_tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        repo = RafflesRepository(get_pool())
        await interaction.response.send_modal(RaffleBuyModal(self.raffle_id, repo))

    @discord.ui.button(label="ℹ️ My Tickets", style=discord.ButtonStyle.secondary)
    async def my_tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        repo = RafflesRepository(get_pool())
        entries = await repo.get_raffle_entries(self.raffle_id)
        mine = [e for e in entries if e.get("discord_id") == interaction.user.id]

        if not mine:
            await interaction.response.send_message("ℹ️ You have no tickets in this raffle yet.", ephemeral=True)
            return

        paid = sum(int(e.get("num_tickets", 0)) for e in mine if e.get("payment_verified"))
        reserved = sum(int(e.get("num_tickets", 0)) for e in mine if not e.get("payment_verified"))
        total = paid + reserved

        info = f"🎟️ **Total tickets:** {total}\n✅ **Confirmed:** {paid}"
        if reserved:
            info += f"\n⏳ **Reserved (unverified):** {reserved}"

        await interaction.response.send_message(info, ephemeral=True)


class PaymentVerificationView(discord.ui.View):
    """View for manually verifying raffle payment."""

    def __init__(self, raffle_id: int, entry_id: int, repo: RafflesRepository, manual: bool = True):
        super().__init__(timeout=300)
        self.raffle_id = raffle_id
        self.entry_id = entry_id
        self.repo = repo
        self.manual = manual

    @discord.ui.button(label="✅ Verify Payment", style=discord.ButtonStyle.green)
    async def verify_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)

        try:
            service = RafflePaymentService(get_database())
            success, sold_out_raffle_id, error = await service.verify_raffle_payment(
                self.entry_id, manual=True
            )

            if not success:
                await interaction.followup.send(
                    f"❌ {error or 'Payment not found. Make sure you sent the items to the creator.'}",
                    ephemeral=True
                )
                return

            if sold_out_raffle_id:
                raffle = await self.repo.get_raffle(sold_out_raffle_id)
                embed = discord.Embed(
                    title="🎉 RAFFLE SOLD OUT!",
                    description=f"🎁 **{raffle['prize']}**\n\n"
                               "All tickets sold! Drawing in **30 seconds**.",
                    color=discord.Color.gold()
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(
                    "✅ Payment verified! Your tickets are confirmed.", ephemeral=True
                )

            self.stop()

        except Exception as e:
            log.error(f"Payment verification error: {e}")
            await interaction.followup.send(
                "❌ Verification failed. Try again.", ephemeral=True
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Reservation cancelled", ephemeral=True
        )
        self.stop()


class RafflesCog(commands.Cog):
    """Raffle commands with sell-out trigger support."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_raffles.start()
        self.cleanup_expired.start()
        self.auto_verify_payments.start()

    def cog_unload(self):
        self.check_raffles.cancel()
        self.cleanup_expired.cancel()
        self.auto_verify_payments.cancel()

    # SINGLE ADMIN-ONLY CREATE COMMAND WITH EMOJIS IN CHOICES
    @app_commands.command(name="raffle_create", description="🎉 Create a new raffle (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def raffle_create(self, interaction: discord.Interaction):
        """🎉 Create a raffle - Admin only."""
        await interaction.response.send_modal(RaffleCreateModal())

    @app_commands.command(name="raffle_draw", description="🎲 Draw a raffle winner (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(raffle_id="🎟️ ID of the raffle to draw")
    async def raffle_draw(self, interaction: discord.Interaction, raffle_id: int):
        """🎲 Manually trigger a raffle draw - Admin only."""
        await interaction.response.defer()

        repo = RafflesRepository(get_pool())
        result = await repo.draw_raffle_winner(raffle_id)

        if not result:
            await interaction.followup.send("❌ No entries or raffle not found")
            return

        # Send winner notification
        verification_cog = self.bot.get_cog("RaffleVerificationCog")
        if verification_cog:
            await verification_cog.send_winner_notification(result)

        embed = discord.Embed(
            title="🎉 RAFFLE WINNER!",
            description=f"🏆 **Winner:** <@{result['discord_id']}>\n"
                       f"🎟️ **Total Entries:** {result['total_entries']}",
            color=discord.Color.gold()
        )

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="raffle_cancel", description="❌ Cancel a raffle (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(raffle_id="🎟️ ID of the raffle to cancel")
    async def raffle_cancel(self, interaction: discord.Interaction, raffle_id: int):
        """❌ Cancel an active raffle - Admin only."""
        repo = RafflesRepository(get_pool())
        
        async with repo.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE raffles SET status = 'cancelled' WHERE raffle_id = $1 AND status = 'active'",
                raffle_id
            )
            
            if result == "UPDATE 0":
                await interaction.response.send_message(
                    "❌ Raffle not found or already completed/cancelled", ephemeral=True
                )
                return
        
        await interaction.response.send_message(
            f"✅ Raffle #{raffle_id} has been cancelled.", ephemeral=True
        )

    @app_commands.command(name="raffle_list", description="📋 List raffles (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def raffle_list(self, interaction: discord.Interaction):
        """📋 List all active raffles - Admin only."""
        repo = RafflesRepository(get_pool())
        raffles = await repo.get_active_raffles(interaction.guild_id)

        if not raffles:
            await interaction.response.send_message(
                "📭 No active raffles", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎉 Active Raffles",
            color=discord.Color.blue()
        )

        for raffle in raffles:
            value = f"🎟️ Tickets: {raffle['tickets_sold']}/{raffle['tickets_available']}\n"
            if raffle.get("is_free") or raffle["ticket_payment_type"] == "free":
                value += "💰 Price: 🎫 FREE"
            elif raffle["ticket_payment_type"] == "xanax":
                value += f"💰 Price: 💊 {raffle['ticket_price']} Xanax"
            else:
                value += f"💰 Price: 📀 {raffle['ticket_price']} Erotic DVD"

            value += "\n⏰ Draw occurs 30 seconds after sellout."

            embed.add_field(
                name=f"#{raffle['raffle_id']}: {raffle['prize'][:50]}",
                value=value,
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(seconds=30)
    async def auto_verify_payments(self):
        """Auto-poll Torn API for payment verification at 4:30 mark."""
        await self.bot.wait_until_ready()

        try:
            repo = RafflesRepository(get_pool())
            pending = await repo.get_pending_verifications()

            for entry in pending:
                try:
                    service = RafflePaymentService(get_database())
                    success, sold_out_id, error = await service.verify_raffle_payment(
                        entry["entry_id"], manual=False
                    )

                    if success:
                        log.info(f"Auto-verified payment for entry {entry['entry_id']}")

                        try:
                            user = await self.bot.fetch_user(entry["discord_id"])
                            await user.send(
                                f"✅ Your raffle tickets for entry #{entry['raffle_id']} have been auto-verified!"
                            )
                        except:
                            pass

                        if sold_out_id:
                            raffle = await repo.get_raffle(sold_out_id)
                            guild = self.bot.get_guild(raffle["guild_id"])
                            if guild and guild.system_channel:
                                embed = discord.Embed(
                                    title="🎉 RAFFLE SOLD OUT!",
                                    description=f"🎁 **{raffle['prize']}** is now sold out! "
                                               "Drawing in **30 seconds**.",
                                    color=discord.Color.gold()
                                )
                                await guild.system_channel.send(embed=embed)

                    elif error and "expired" in error.lower():
                        await repo.cancel_expired_reservation(entry["entry_id"])
                        log.info(f"Cancelled expired reservation {entry['entry_id']}")

                except Exception as e:
                    log.error(f"Auto-verify error for entry {entry['entry_id']}: {e}")

        except Exception as e:
            log.error(f"Error in auto_verify_payments task: {e}")

    @tasks.loop(minutes=1)
    async def check_raffles(self):
        """Check for raffles that need to be drawn."""
        await self.bot.wait_until_ready()

        try:
            repo = RafflesRepository(get_pool())
            raffles = await repo.get_raffles_to_draw()

            for raffle in raffles:
                try:
                    result = await repo.draw_raffle_winner(raffle["raffle_id"])

                    if result:
                        verification_cog = self.bot.get_cog("RaffleVerificationCog")
                        if verification_cog:
                            await verification_cog.send_winner_notification(result)

                        guild = self.bot.get_guild(raffle["guild_id"])
                        if guild and guild.system_channel:
                            embed = discord.Embed(
                                title="🎉 RAFFLE WINNER!",
                                description=f"🎁 **{raffle['prize']}**\n\n"
                                           f"🏆 Winner: <@{result['discord_id']}>\n"
                                           f"🎟️ Total Entries: {result['total_entries']}",
                                color=discord.Color.gold()
                            )
                            await guild.system_channel.send(embed=embed)

                except Exception as e:
                    log.error(f"Error drawing raffle {raffle['raffle_id']}: {e}")

        except Exception as e:
            log.error(f"Error in check_raffles task: {e}")

    @tasks.loop(minutes=5)
    async def cleanup_expired(self):
        """Clean up expired unpaid reservations."""
        try:
            repo = RafflesRepository(get_pool())
            count = await repo.cleanup_expired_raffle_entries()
            if count > 0:
                log.info(f"Cleaned up {count} expired raffle entries")
        except Exception as e:
            log.error(f"Error cleaning up expired entries: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RafflesCog(bot))
