"""
Raffle prize verification system with Torn API integration.
"""
import discord
from discord import ui
from discord.ext import commands

from repositories.raffles import RafflesRepository
from utils.database import get_pool


class VerifyPrizeButton(ui.Button):
    def __init__(self, raffle_id: int):
        super().__init__(
            label="Verify Prize Received",
            style=discord.ButtonStyle.green,
            emoji="✅",
            custom_id=f"verify_prize_{raffle_id}"
        )
        self.raffle_id = raffle_id
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        repo = RafflesRepository(get_pool())
        
        raffle = await repo.get_raffle(self.raffle_id)

        if not raffle or raffle["status"] != 'awaiting_delivery':
                await interaction.followup.send(
                    "❌ This raffle is not awaiting delivery verification.", 
                    ephemeral=True
                )
                return
            
        if raffle["prize_verified_at"]:
                await interaction.followup.send(
                    "✅ Prize already verified!", 
                    ephemeral=True
                )
                return
        
        # Verify using winner's API key (decrypted from DB)
        result = await repo.verify_prize_delivery(
            raffle_id=self.raffle_id,
            winner_discord_id=interaction.user.id,  # The person clicking is the winner
            winner_torn_id=raffle["winner_torn_id"],
            creator_torn_id=raffle["creator_torn_id"],
        )
        
        if result.get("verified"):
            embed = discord.Embed(
                title="🎉 Prize Verified!",
                description=f"Log #{result['log_id']} confirms item received from raffle creator.",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Log Details",
                value=f"Time: <t:{result['timestamp']}:R>\nItem: {result['log_entry'].get('title', 'Unknown')}",
                inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Disable button
            self.disabled = True
            self.label = "Prize Verified ✅"
            await interaction.message.edit(view=self.view)
            
        elif "error" in result:
            await interaction.followup.send(
                f"❌ Error: {result['error']}", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ {result.get('message', 'Verification failed')}\n\n"
                "Make sure the creator has sent you the item and try again.", 
                ephemeral=True
            )


class RaffleVerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    async def send_winner_notification(self, winner_data: dict):
        """Send DM to winner with verification button."""
        try:
            user = await self.bot.fetch_user(winner_data["discord_id"])
            
            embed = discord.Embed(
                title="🎉 Congratulations! You Won!",
                description=f"You won: **{winner_data['prize']}**",
                color=discord.Color.gold()
            )
            winner_torn = winner_data.get('torn_name') or f"ID {winner_data.get('torn_user_id', 'N/A')}"
            embed.add_field(
                name="Winner Details",
                value=f"Winning Entry: #1\n"
                      f"Your Torn: {winner_torn} [{winner_data.get('torn_user_id', 'N/A')}]\n"
                      f"Total Entries: {winner_data.get('total_entries', 0)}",
                inline=False
            )
            embed.add_field(
                name="Next Steps",
                value="The raffle creator will send your prize soon.\n"
                      "Click the button below once you receive it!",
                inline=False
            )
            
            view = ui.View(timeout=None)
            view.add_item(VerifyPrizeButton(winner_data["raffle_id"]))
            
            await user.send(embed=embed, view=view)
            
        except discord.Forbidden:
            # Can't DM user - could log to a channel instead
            pass
        except Exception as e:
            print(f"Error sending winner notification: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RaffleVerificationCog(bot))
