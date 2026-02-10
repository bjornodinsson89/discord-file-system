"""
Raffle system with sell-out trigger support and automatic payment verification.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.raffles import RafflesRepository
from utils.database import get_pool

log = logging.getLogger("happy_jumper.raffles")


class RaffleCreateModal(discord.ui.Modal):
    """Modal for creating a new raffle."""

    prize = discord.ui.TextInput(
        label="🎁 Prize",
        placeholder="What are you giving away?",
        required=True,
        max_length=200
    )

    ticket_price = discord.ui.TextInput(
        label="💰 Ticket Price",
        placeholder="Number of xanax/dvds per ticket (0 for free)",
        required=True,
        max_length=3,
        default="1"
    )

    tickets_available = discord.ui.TextInput(
        label="🎟️ Total Tickets Available",
        placeholder="Max tickets to sell (min 10)",
        required=True,
        max_length=4
    )

    max_per_user = discord.ui.TextInput(
        label="📋 Max Tickets Per User",
        placeholder="0 = unlimited",
        required=False,
        max_length=3,
        default="0"
    )

    def __init__(self, payment_type: str, end_trigger: str, hours: int, minutes: int):
        super().__init__(title="🎉 Create Raffle")
        self.payment_type = payment_type
        self.end_trigger = end_trigger
        self.hours = hours
        self.minutes = minutes

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.ticket_price.value) if self.ticket_price.value else 0
            total = int(self.tickets_available.value)
            max_per = int(self.max_per_user.value or 0)

            if total < 10:
                await interaction.response.send_message(
                    "❌ Minimum 10 tickets required", ephemeral=True
                )
                return

        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid numbers provided", ephemeral=True
            )
            return

        # Calculate timing
        if self.end_trigger == "time":
            end_time = datetime.utcnow() + timedelta(hours=self.hours, minutes=self.minutes)
            hours_after_sold_out = None
        else:
            end_time = datetime.utcnow() + timedelta(days=30)
            hours_after_sold_out = self.hours + (self.minutes / 60)

        # Force price to 0 for free entries
        actual_price = 0 if self.payment_type == "free" else price

        repo = RafflesRepository(get_pool())

        try:
            raffle_id = await repo.create_raffle(
                guild_id=interaction.guild_id,
                creator_discord_id=interaction.user.id,
                prize=self.prize.value,
                ticket_payment_type=self.payment_type,
                ticket_price=actual_price,
                tickets_available=total,
                max_tickets_per_user=max_per,
                end_time=end_time,
                end_trigger=self.end_trigger,
                hours_after_sold_out=hours_after_sold_out
            )

            # Build display with emojis
            if self.payment_type == "free":
                price_display = "🎫 FREE"
            elif self.payment_type == "xanax":
                price_display = f"💊 {actual_price} Xanax"
            else:
                price_display = f"📀 {actual_price} Erotic DVD"

            embed = discord.Embed(
                title="🎉 New Raffle Created!",
                description=f"🎁 **Prize:** {self.prize.value}\n"
                           f"🎟️ **Tickets:** {total} available\n"
                           f"💰 **Price:** {price_display} per ticket\n"
                           f"📋 **Max per user:** {'Unlimited ♾️' if max_per == 0 else max_per}",
                color=discord.Color.green()
            )

            if self.end_trigger == "tickets_sold":
                time_str = f"{self.hours}h {self.minutes}m" if self.minutes else f"{self.hours}h"
                if self.hours == 0:
                    time_str = f"{self.minutes}m"
                embed.add_field(
                    name="⏰ End Condition",
                    value=f"🎟️ When sold out + **{time_str}**",
                    inline=False
                )
            else:
                time_str = []
                if self.hours > 0:
                    time_str.append(f"{self.hours}h")
                if self.minutes > 0:
                    time_str.append(f"{self.minutes}m")
                embed.add_field(
                    name="⏰ End Time",
                    value=f"⏱️ {' '.join(time_str)} from now",
                    inline=False
                )

            if self.payment_type == "free":
                embed.add_field(
                    name="✨ Free Entry",
                    value="🎫 No payment required! Click the button on the raffle card to enter.",
                    inline=False
                )

            await interaction.response.send_message(embed=embed)

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
        max_length=2
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
        reserved_until = datetime.utcnow() + timedelta(minutes=5)

        try:
            entry = await self.repo.reserve_entry(
                raffle_id=self.raffle_id,
                discord_id=interaction.user.id,
                torn_user_id=0,
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
            success, sold_out_raffle_id, error = await self.repo.verify_payment_and_check_sold_out(
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
                               f"All tickets sold! Drawing in **{raffle['hours_after_sold_out']} hours**.",
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
    @app_commands.describe(
        payment_type="💰 Payment type for tickets",
        payment_amount="💵 Price per ticket (set to 0 for free)",
        ticket_amount="🎟️ Total tickets available (min 10)",
        max_per_user="📋 Max tickets per user (0 = unlimited)",
        prize="🎁 What is the prize?",
        trigger="⏰ How the raffle ends",
        hours="⏱️ Hours (duration for time, delay for sell-out)",
        minutes="⏱️ Minutes (duration for time, delay for sell-out)"
    )
    @app_commands.choices(payment_type=[
        app_commands.Choice(name="🎫 Free Entry", value="free"),
        app_commands.Choice(name="💊 Xanax", value="xanax"),
        app_commands.Choice(name="📀 Erotic DVD", value="erotic_dvd")
    ])
    @app_commands.choices(trigger=[
        app_commands.Choice(name="⏰ Time Based", value="time"),
        app_commands.Choice(name="🎟️ Sell Out + Delay", value="tickets_sold")
    ])
    async def raffle_create(
        self,
        interaction: discord.Interaction,
        payment_type: app_commands.Choice[str],
        payment_amount: app_commands.Range[int, 0, 1000],
        ticket_amount: app_commands.Range[int, 10, 10000],
        max_per_user: app_commands.Range[int, 0, 100],
        prize: str,
        trigger: app_commands.Choice[str],
        hours: app_commands.Range[int, 0, 48],
        minutes: app_commands.Range[int, 0, 59]
    ):
        """🎉 Create a raffle - Admin only."""
        if hours == 0 and minutes == 0:
            await interaction.response.send_message(
                "❌ Please specify at least 1 hour or 1 minute", ephemeral=True
            )
            return

        modal = RaffleCreateModal(
            payment_type=payment_type.value,
            end_trigger=trigger.value,
            hours=hours,
            minutes=minutes
        )
        
        # Pre-fill the modal values from command args
        modal.prize.default = prize
        modal.ticket_price.default = str(payment_amount) if payment_type.value != "free" else "0"
        modal.tickets_available.default = str(ticket_amount)
        modal.max_per_user.default = str(max_per_user)
        
        await interaction.response.send_modal(modal)

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
            description=f"🏆 **Winner:** {result['torn_name']} [{result['torn_user_id']}]\n"
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

            if raffle["end_trigger"] == "tickets_sold":
                hours = int(raffle["hours_after_sold_out"])
                mins = int((raffle["hours_after_sold_out"] % 1) * 60)
                time_str = f"{hours}h {mins}m" if mins else f"{hours}h"
                value += f"\n⏰ Trigger: 🎟️ Sell-out + {time_str}"
            else:
                value += f"\n⏰ Ends: <t:{int(raffle['end_time'].timestamp())}:R>"

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
                    success, sold_out_id, error = await repo.verify_payment_and_check_sold_out(
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
                                               f"Drawing in **{raffle['hours_after_sold_out']} hours**.",
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
                                           f"🏆 Winner: {result['torn_name']} [{result['torn_user_id']}]\n"
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
