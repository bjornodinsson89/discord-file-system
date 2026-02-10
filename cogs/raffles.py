"""
Raffle system with sell-out trigger support, automatic payment verification, and free entries.
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
        label="Prize",
        placeholder="What are you giving away?",
        required=True,
        max_length=200
    )

    ticket_price = discord.ui.TextInput(
        label="Ticket Price",
        placeholder="Number of xanax/dvds per ticket (ignored for free)",
        required=False,
        max_length=3,
        default="1"
    )

    tickets_available = discord.ui.TextInput(
        label="Total Tickets Available",
        placeholder="Max tickets to sell (min 10)",
        required=True,
        max_length=4
    )

    max_per_user = discord.ui.TextInput(
        label="Max Tickets Per User",
        placeholder="0 = unlimited",
        required=False,
        max_length=3,
        default="0"
    )

    duration = discord.ui.TextInput(
        label="Duration (if time-based)",
        placeholder="e.g., '2h30m' or leave blank for sell-out only",
        required=False,
        max_length=10,
        default=""
    )

    def __init__(
        self,
        payment_type: str,
        end_trigger: str,
        hours_after_sold_out: Optional[int] = None
    ):
        super().__init__(title="Create Raffle")
        self.payment_type = payment_type
        self.end_trigger = end_trigger
        self.hours_after_sold_out = hours_after_sold_out
        self.end_time = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            total = int(self.tickets_available.value)
            max_per = int(self.max_per_user.value or 0)

            if total < 10:
                await interaction.response.send_message(
                    "❌ Minimum 10 tickets required", ephemeral=True
                )
                return

            # Parse price (ignored if free)
            if self.payment_type == "free":
                price = 0
            else:
                price = int(self.ticket_price.value or 1)

            # Parse duration for time-based triggers
            if self.end_trigger == "time":
                duration_str = self.duration.value.strip()
                if not duration_str:
                    await interaction.response.send_message(
                        "❌ Duration required for time-based raffles (e.g., '2h30m')", 
                        ephemeral=True
                    )
                    return
                
                hours, minutes = self._parse_duration(duration_str)
                if hours is None:
                    await interaction.response.send_message(
                        "❌ Invalid duration format. Use like '2h30m', '1h', '30m'", 
                        ephemeral=True
                    )
                    return
                
                self.end_time = datetime.utcnow() + timedelta(hours=hours, minutes=minutes)
            else:
                self.end_time = datetime.utcnow() + timedelta(days=365)  # Long default for sell-out

        except ValueError as e:
            await interaction.response.send_message(
                f"❌ Invalid input: {e}", ephemeral=True
            )
            return

        repo = RafflesRepository(get_pool())

        try:
            raffle_id = await repo.create_raffle(
                guild_id=interaction.guild_id,
                creator_discord_id=interaction.user.id,
                prize=self.prize.value,
                ticket_payment_type=self.payment_type,
                ticket_price=price,
                tickets_available=total,
                max_tickets_per_user=max_per,
                end_time=self.end_time,
                end_trigger=self.end_trigger,
                hours_after_sold_out=self.hours_after_sold_out
            )

            # Build price display
            if self.payment_type == "free":
                price_display = "FREE"
            else:
                price_display = f"{price} {self.payment_type}"

            embed = discord.Embed(
                title="🎉 New Raffle Created!",
                description=f"Prize: **{self.prize.value}**\n"
                           f"Tickets: **{total}** available\n"
                           f"Price: **{price_display}** per ticket\n"
                           f"Max per user: **{'Unlimited' if max_per == 0 else max_per}**",
                color=discord.Color.green()
            )

            if self.payment_type == "free":
                embed.add_field(
                    name="🎫 Free Entry",
                    value="No payment required! Just click to enter.",
                    inline=False
                )
            elif self.end_trigger == "tickets_sold":
                embed.add_field(
                    name="⏰ End Condition",
                    value=f"When sold out + **{self.hours_after_sold_out} hours**",
                    inline=False
                )
            else:
                time_str = self._format_duration(hours, minutes) if self.end_trigger == "time" else "N/A"
                embed.add_field(
                    name="⏰ End Time",
                    value=f"{time_str} from now",
                    inline=False
                )

            embed.add_field(
                name="🎫 How to Enter",
                value=f"Use `/raffle buy {raffle_id}` to enter!",
                inline=False
            )

            await interaction.response.send_message(embed=embed)

        except Exception as e:
            log.error(f"Failed to create raffle: {e}")
            await interaction.response.send_message(
                "❌ Failed to create raffle", ephemeral=True
            )

    def _parse_duration(self, s: str):
        """Parse '2h30m' into (2, 30). Returns (None, None) if invalid."""
        import re
        match = re.match(r'(?:(\d+)h)?(?:(\d+)m?)?$', s.lower().replace(' ', ''))
        if not match:
            return None, None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        if hours == 0 and minutes == 0:
            return None, None
        return hours, minutes

    def _format_duration(self, hours: int, minutes: int) -> str:
        parts = []
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        return ' '.join(parts) if parts else "immediately"


class RaffleBuyModal(discord.ui.Modal):
    """Modal for buying raffle tickets."""

    quantity = discord.ui.TextInput(
        label="Number of Tickets",
        placeholder="How many tickets?",
        required=True,
        max_length=2
    )

    def __init__(self, raffle_id: int, repo: RafflesRepository):
        super().__init__(title="Buy Raffle Tickets")
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

        # Check raffle exists and is active
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

        # Handle free entry
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
                    description=f"Raffle: **{raffle['prize']}**\n"
                               f"Tickets: **{quantity}**\n"
                               f"Price: **FREE**",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Status",
                    value="Your entry is confirmed! Good luck!",
                    inline=False
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

        # Paid entry flow (existing)
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

            embed = discord.Embed(
                title="🎫 Tickets Reserved!",
                description=f"Raffle: **{raffle['prize']}**\n"
                           f"Tickets: **{quantity}**\n"
                           f"Total cost: **{total_cost} {raffle['ticket_payment_type']}**",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="⏰ Payment Deadline",
                value=f"Auto-verification at 4:30, expires at 5:00\nSend **{total_cost} {raffle['ticket_payment_type']}** to raffle creator in-game!",
                inline=False
            )
            embed.add_field(
                name="How to Pay",
                value="Send items via Torn, bot will auto-detect. Click 'Verify Now' to check early.",
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

    @discord.ui.button(label="Verify Payment Now", style=discord.ButtonStyle.green, emoji="✅")
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
                    description=f"**{raffle['prize']}**\n\n"
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

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Reservation cancelled", ephemeral=True
        )
        self.stop()


class RafflesCog(commands.Cog):
    """Raffle commands with sell-out trigger support and free entries."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_raffles.start()
        self.cleanup_expired.start()
        self.auto_verify_payments.start()

    def cog_unload(self):
        self.check_raffles.cancel()
        self.cleanup_expired.cancel()
        self.auto_verify_payments.cancel()

    raffle_group = app_commands.Group(
        name="raffle",
        description="Raffle management commands"
    )

    @raffle_group.command(name="create", description="Create a new raffle")
    @app_commands.describe(
        payment_type="Payment type (Free = no cost)",
        end_trigger="How the raffle ends",
        delay_hours="Hours after sell-out before drawing (only for sell-out trigger)",
        duration="Duration like '2h30m' (only for time trigger)"
    )
    @app_commands.choices(payment_type=[
        app_commands.Choice(name="🎫 Free Entry", value="free"),
        app_commands.Choice(name="💊 Xanax", value="xanax"),
        app_commands.Choice(name="📀 Erotic DVD", value="erotic_dvd")
    ])
    @app_commands.choices(end_trigger=[
        app_commands.Choice(name="⏰ Time Based", value="time"),
        app_commands.Choice(name="🎟️ Sell Out + Delay", value="tickets_sold")
    ])
    async def create_raffle(
        self,
        interaction: discord.Interaction,
        payment_type: app_commands.Choice[str],
        end_trigger: app_commands.Choice[str],
        delay_hours: Optional[app_commands.Range[int, 0, 48]] = None,
        duration: Optional[str] = None
    ):
        """Create a raffle - choose between free entry or paid, and time vs sell-out trigger."""
        
        # Validate parameters based on trigger type
        if end_trigger.value == "tickets_sold":
            if delay_hours is None or delay_hours < 1:
                await interaction.response.send_message(
                    "❌ Sell-out raffles require delay_hours (1-48)", ephemeral=True
                )
                return
        elif end_trigger.value == "time":
            if not duration:
                await interaction.response.send_message(
                    "❌ Time-based raffles require a duration (e.g., '2h30m')", ephemeral=True
                )
                return

        modal = RaffleCreateModal(
            payment_type=payment_type.value,
            end_trigger=end_trigger.value,
            hours_after_sold_out=delay_hours if end_trigger.value == "tickets_sold" else None
        )
        await interaction.response.send_modal(modal)

    @raffle_group.command(name="buy", description="Buy raffle tickets or enter free raffle")
    @app_commands.describe(raffle_id="ID of the raffle to enter")
    async def buy_tickets(self, interaction: discord.Interaction, raffle_id: int):
        """Buy tickets for an active raffle or enter free raffle."""
        repo = RafflesRepository(get_pool())
        modal = RaffleBuyModal(raffle_id, repo)
        await interaction.response.send_modal(modal)

    @raffle_group.command(name="view", description="View raffle details")
    @app_commands.describe(raffle_id="Raffle ID to view")
    async def view_raffle(self, interaction: discord.Interaction, raffle_id: int):
        """View details of a specific raffle."""
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)

        if not raffle:
            await interaction.response.send_message(
                "❌ Raffle not found", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🎉 {raffle['prize']}",
            color=discord.Color.blue()
        )

        status_emoji = "🎫" if raffle["is_free"] else "💰"
        embed.add_field(
            name="Type",
            value=f"{status_emoji} {'Free Entry' if raffle['is_free'] else raffle['ticket_payment_type'].title()}",
            inline=True
        )
        
        embed.add_field(
            name="Status",
            value=raffle["status"].title(),
            inline=True
        )
        embed.add_field(
            name="Tickets",
            value=f"{raffle['tickets_sold']}/{raffle['tickets_available']}",
            inline=True
        )

        if not raffle["is_free"]:
            embed.add_field(
                name="Price",
                value=f"{raffle['ticket_price']} {raffle['ticket_payment_type']}",
                inline=True
            )

        if raffle["end_trigger"] == "tickets_sold":
            if raffle["tickets_fully_sold_at"]:
                draw_time = raffle["tickets_fully_sold_at"] + timedelta(
                    hours=raffle["hours_after_sold_out"]
                )
                embed.add_field(
                    name="⏰ Drawing",
                    value=f"<t:{int(draw_time.timestamp())}:R>",
                    inline=False
                )
            else:
                embed.add_field(
                    name="⏰ Drawing",
                    value=f"{raffle['hours_after_sold_out']}h after sell-out",
                    inline=False
                )
        else:
            embed.add_field(
                name="⏰ Ends",
                value=f"<t:{int(raffle['end_time'].timestamp())}:R>",
                inline=False
            )

        if raffle["winner_discord_id"]:
            winner_name = raffle.get("winner_torn_name", "Unknown")
            embed.add_field(
                name="🏆 Winner",
                value=f"{winner_name} [{raffle['winner_torn_id']}]",
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    @raffle_group.command(name="list", description="List active raffles")
    async def list_raffles(self, interaction: discord.Interaction):
        """List all active raffles in this guild."""
        repo = RafflesRepository(get_pool())
        raffles = await repo.get_active_raffles(interaction.guild_id)

        if not raffles:
            await interaction.response.send_message(
                "No active raffles", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎉 Active Raffles",
            color=discord.Color.blue()
        )

        for raffle in raffles:
            price_str = "FREE" if raffle["is_free"] else f"{raffle['ticket_price']} {raffle['ticket_payment_type']}"
            value = f"Tickets: {raffle['tickets_sold']}/{raffle['tickets_available']}\n"
            value += f"Price: {price_str}"

            if raffle["end_trigger"] == "tickets_sold":
                value += f"\nTrigger: Sell-out + {raffle['hours_after_sold_out']}h"
            else:
                value += f"\nEnds: <t:{int(raffle['end_time'].timestamp())}:R>"

            emoji = "🎫" if raffle["is_free"] else "🎟️"
            embed.add_field(
                name=f"{emoji} #{raffle['raffle_id']}: {raffle['prize'][:50]}",
                value=value,
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    @raffle_group.command(name="draw", description="Manually draw a raffle winner (Admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def manual_draw(self, interaction: discord.Interaction, raffle_id: int):
        """Manually trigger a raffle draw."""
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
            description=f"Winner: {result['torn_name']} [{result['torn_user_id']}]\n"
                       f"Total Entries: {result['total_entries']}",
            color=discord.Color.gold()
        )

        await interaction.followup.send(embed=embed)

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
                                    description=f"**{raffle['prize']}** is now sold out! "
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
                                description=f"**{raffle['prize']}**\n\n"
                                           f"Winner: {result['torn_name']} [{result['torn_user_id']}]\n"
                                           f"Total Entries: {result['total_entries']}",
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
