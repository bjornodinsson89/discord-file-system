from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from repositories.engagement import EngagementRepository
from repositories.prize_tokens import PrizeTokensRepository
from services.engagement_service import EngagementService, required_total_xp
from services.prize_token_service import PrizeTokenService
from utils.database import get_pool


class EngagementCog(commands.Cog):
    profile = app_commands.Group(name="profile", description="View engagement profiles")
    tokens = app_commands.Group(name="tokens", description="Prize token commands")
    engagement = app_commands.Group(name="engagement", description="Engagement admin tools")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        pool = get_pool()
        self.repo = EngagementRepository(pool)
        self.token_repo = PrizeTokensRepository(pool)
        self.token_service = PrizeTokenService(self.token_repo)
        self.service = EngagementService(self.repo, self.token_service)

    async def _profile_for(self, guild_id: int, user_id: int) -> dict:
        return await self.repo.get_or_create_profile(guild_id, user_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if getattr(message, "interaction", None) is not None or message.content.strip().startswith("/"):
            return
        await self.service.message_xp_if_eligible(
            guild_id=message.guild.id,
            user_id=message.author.id,
            content=message.content,
            channel_id=message.channel.id,
            role_ids=[r.id for r in getattr(message.author, "roles", [])],
            category_id=getattr(getattr(message.channel, "category", None), "id", None),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        reactor = guild.get_member(payload.user_id)
        if reactor is None or reactor.bot:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return
        target = message.author
        if target.bot or target.id == reactor.id:
            return
        await self.service.reaction_xp_if_eligible(
            guild_id=guild.id,
            reactor_user_id=reactor.id,
            target_user_id=target.id,
            message_id=payload.message_id,
        )

    @commands.Cog.listener()
    async def on_paid_raffle_purchase_verified(self, payload: dict):
        await self.service.process_paid_raffle_purchase(payload)

    @commands.Cog.listener()
    async def on_raffle_prize_token_purchase_confirmed(self, payload: dict):
        await self.service.process_raffle_prize_token_purchase_confirmed(payload)

    @commands.Cog.listener()
    async def on_jump_99k_purchase_verified(self, payload: dict):
        await self.service.process_jump_purchase_verified(payload)

    @commands.Cog.listener()
    async def on_jump_99k_completed(self, payload: dict):
        await self.service.process_jump_completed(payload)

    @commands.Cog.listener()
    async def on_giveaway_joined(self, _payload: dict):
        return

    @profile.command(name="view", description="View detailed profile stats")
    async def profile_view(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        p = await self._profile_for(interaction.guild.id, target.id)
        level = int(p.get("level") or 0)
        xp_total = int(p.get("xp_total") or 0)
        next_xp = required_total_xp(level + 1)
        left = max(0, next_xp - xp_total)
        lines = [
            f"Level: **{level}**",
            f"XP Total: **{xp_total}**",
            f"XP to Next: **{left}**",
            f"Prize Tokens: **{int(p.get('prize_token_balance') or 0)}**",
            f"Lifetime Earned/Spent: **{int(p.get('prize_token_lifetime_earned') or 0)} / {int(p.get('prize_token_lifetime_spent') or 0)}**",
            f"Message XP: **{int(p.get('message_xp_total') or 0)}**",
            f"Reaction XP: **{int(p.get('reaction_xp_total') or 0)}**",
            f"Paid Raffle XP: **{int(p.get('paid_raffle_xp_total') or 0)}**",
            f"Jump Purchase XP: **{int(p.get('jump_purchase_xp_total') or 0)}**",
            f"Jump Completion XP: **{int(p.get('jump_completion_xp_total') or 0)}**",
            f"Paid Raffle Purchases/Tickets: **{int(p.get('paid_raffle_purchases_count') or 0)} / {int(p.get('paid_raffle_tickets_count') or 0)}**",
            f"Jump Purchases/Completions: **{int(p.get('jump_99k_purchases_count') or 0)} / {int(p.get('jump_99k_completed_count') or 0)}**",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @profile.command(name="rank", description="View compact rank")
    async def profile_rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        p = await self._profile_for(interaction.guild.id, target.id)
        rank = await self.repo.get_rank(interaction.guild.id, target.id)
        level = int(p.get("level") or 0)
        xp_total = int(p.get("xp_total") or 0)
        cur = required_total_xp(level)
        nxt = required_total_xp(level + 1)
        pct = 0 if nxt <= cur else int(((xp_total - cur) / (nxt - cur)) * 100)
        bar_fill = max(0, min(10, int(pct / 10)))
        bar = "█" * bar_fill + "░" * (10 - bar_fill)
        await interaction.response.send_message(
            f"Rank #{rank} • L{level} • `{bar}` {pct}%\nXP: {xp_total} • Tokens: {int(p.get('prize_token_balance') or 0)}",
            ephemeral=True,
        )

    @tokens.command(name="balance", description="View token balance")
    async def tokens_balance(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        p = await self._profile_for(interaction.guild.id, target.id)
        await interaction.response.send_message(
            f"Balance: **{int(p.get('prize_token_balance') or 0)}**\n"
            f"Lifetime earned: **{int(p.get('prize_token_lifetime_earned') or 0)}**\n"
            f"Lifetime spent: **{int(p.get('prize_token_lifetime_spent') or 0)}**",
            ephemeral=True,
        )

    @engagement.command(name="debug", description="Debug engagement state")
    @app_commands.checks.has_permissions(administrator=True)
    async def engagement_debug(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        p = await self._profile_for(interaction.guild.id, target.id)
        message_state = await self.repo.get_message_state(interaction.guild.id, target.id)
        events = await self.repo.get_recent_event_rows(interaction.guild.id, target.id, limit=5)
        txs = await self.token_repo.get_recent_transactions(interaction.guild.id, target.id, limit=5)
        await interaction.response.send_message(
            f"Profile: {p}\nMessage state: {message_state}\nRecent events: {events}\nRecent token tx: {txs}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = EngagementCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.profile)
    bot.tree.add_command(cog.tokens)
    bot.tree.add_command(cog.engagement)
