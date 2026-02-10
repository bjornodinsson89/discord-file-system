from discord.ext import commands


class RafflesCog(commands.Cog):
    """Raffle command namespace placeholder for Phase 3 modular loading."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(RafflesCog(bot))
