from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.engagement import EngagementRepository
from repositories.prize_tokens import PrizeTokensRepository
from services.engagement_service import EngagementService, required_total_xp
from services.prize_token_service import PrizeTokenService
from utils.database import get_pool


class EngagementCog(commands.Cog):
    profile = app_commands.Group(name="profile", description="View engagement profiles")
    tokens = app_commands.Group(name="tokens", description="Prize token commands")
    engagement = app_commands.Group(name="engagement", description="Engagement admin tools")
    leaderboard = app_commands.Group(name="leaderboard", description="Engagement leaderboards")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        pool = get_pool()
        self.repo = EngagementRepository(pool)
        self.token_repo = PrizeTokensRepository(pool)
        self.token_service = PrizeTokenService(self.token_repo)
        self.service = EngagementService(self.repo, self.token_service)
        self.voice_xp_worker.start()

    def cog_unload(self):
        self.voice_xp_worker.cancel()

    async def _profile_for(self, guild_id: int, user_id: int) -> dict:
        return await self.repo.get_or_create_profile(guild_id, user_id)

    async def _post_levelup_announcement(self, guild_id: int, user_id: int, level: int) -> None:
        try:
            settings = await self.repo.get_or_create_guild_settings(guild_id)
            channel_id = int(settings.get("levelup_channel_id") or 0)
            if not channel_id:
                return
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            member = guild.get_member(user_id)
            if member is None:
                return
            channel = guild.get_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                return
            await channel.send(f"🎉 {member.mention} reached Level {level} and earned 1 Prize Token.")
        except Exception:
            return

    async def _run_voice_tick(self) -> None:
        minute_bucket = int(datetime.now(timezone.utc).timestamp()) // 60
        for guild in list(self.bot.guilds):
            try:
                settings = await self.repo.get_or_create_guild_settings(guild.id)
                if not settings.get("enabled") or not settings.get("voice_xp_enabled"):
                    continue
                afk_channel_id = getattr(guild.afk_channel, "id", None)
                for channel in guild.voice_channels:
                    if afk_channel_id and channel.id == afk_channel_id:
                        continue
                    humans = [m for m in channel.members if not m.bot]
                    if len(humans) < 2:
                        continue
                    for member in humans:
                        voice_state = getattr(member, "voice", None)
                        if voice_state is None:
                            continue
                        if bool(getattr(voice_state, "self_mute", False)) and bool(getattr(voice_state, "self_deaf", False)):
                            continue
                        await self.service.voice_xp_if_eligible(
                            guild_id=guild.id,
                            user_id=member.id,
                            channel_id=channel.id,
                            role_ids=[r.id for r in getattr(member, "roles", [])],
                            category_id=getattr(getattr(channel, "category", None), "id", None),
                            minute_bucket=minute_bucket,
                            on_level_up=self._post_levelup_announcement,
                        )
            except Exception:
                continue

    @tasks.loop(seconds=60)
    async def voice_xp_worker(self):
        await self._run_voice_tick()

    @voice_xp_worker.before_loop
    async def before_voice_xp_worker(self):
        await self.bot.wait_until_ready()

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
            on_level_up=self._post_levelup_announcement,
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
            on_level_up=self._post_levelup_announcement,
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
            f"Voice XP: **{int(p.get('voice_xp_total') or 0)}**",
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

    @profile.command(name="rewards", description="View rewards progress")
    async def profile_rewards(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        p = await self._profile_for(interaction.guild.id, target.id)
        level = int(p.get("level") or 0)
        xp_total = int(p.get("xp_total") or 0)
        next_xp = required_total_xp(level + 1)
        left = max(0, next_xp - xp_total)
        await interaction.response.send_message(
            f"Level: **{level}**\n"
            f"XP Total: **{xp_total}**\n"
            f"Prize Token Balance: **{int(p.get('prize_token_balance') or 0)}**\n"
            f"XP needed for next level: **{left}**\n"
            "Reward roles are not configured yet.",
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

    @tokens.command(name="history", description="View recent token transactions")
    async def tokens_history(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        txs = await self.token_repo.get_recent_transactions(interaction.guild.id, target.id, limit=10)
        if not txs:
            await interaction.response.send_message("No token history yet.", ephemeral=True)
            return
        lines = []
        for tx in txs:
            lines.append(
                f"`{tx.get('id')}` {tx.get('transaction_type')}: {int(tx.get('amount') or 0):+d} → {int(tx.get('balance_after') or 0)}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _send_leaderboard(self, interaction: discord.Interaction, board: str, title: str, value_key: str):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.repo.get_leaderboard(interaction.guild.id, board, limit=10)
        if not rows:
            await interaction.response.send_message("No data yet.", ephemeral=True)
            return
        lines = []
        for idx, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(int(row["user_id"]))
            name = member.display_name if member else f"User {row['user_id']}"
            lines.append(f"{idx}. **{name}** — {int(row.get(value_key) or 0)}")
        await interaction.response.send_message(f"**{title}**\n" + "\n".join(lines), ephemeral=True)

    @leaderboard.command(name="xp", description="Top users by XP")
    async def leaderboard_xp(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, "xp", "XP Leaderboard", "xp_total")

    @leaderboard.command(name="levels", description="Top users by level")
    async def leaderboard_levels(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, "levels", "Levels Leaderboard", "level")

    @leaderboard.command(name="tokens", description="Top users by lifetime earned tokens")
    async def leaderboard_tokens(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, "tokens", "Tokens Leaderboard", "prize_token_lifetime_earned")

    @leaderboard.command(name="jumps", description="Top users by completed jumps")
    async def leaderboard_jumps(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, "jumps", "Jumps Leaderboard", "jump_99k_completed_count")

    @leaderboard.command(name="raffles", description="Top users by paid raffle tickets")
    async def leaderboard_raffles(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, "raffles", "Raffles Leaderboard", "paid_raffle_tickets_count")

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

    @engagement.command(name="config", description="Show engagement config")
    @app_commands.checks.has_permissions(administrator=True)
    async def engagement_config(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        s = await self.repo.get_or_create_guild_settings(interaction.guild.id)
        await interaction.response.send_message(
            "\n".join(
                [
                    "**Engagement Config**",
                    f"Enabled: `{bool(s.get('enabled'))}`",
                    f"Level-up channel: `{s.get('levelup_channel_id') or 'Not set'}`",
                    f"Leaderboards enabled: `{bool(s.get('leaderboard_enabled'))}`",
                    f"Profile cards enabled: `{bool(s.get('profile_cards_enabled'))}`",
                    f"Message XP: `{bool(s.get('message_xp_enabled'))}`",
                    f"Reaction XP: `{bool(s.get('reaction_xp_enabled'))}`",
                    f"Voice XP: `{bool(s.get('voice_xp_enabled'))}`",
                    f"Ignored channels: `{s.get('ignored_channel_ids_json') or []}`",
                    f"Ignored categories: `{s.get('ignored_category_ids_json') or []}`",
                    f"Ignored roles: `{s.get('ignored_role_ids_json') or []}`",
                ]
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = EngagementCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.profile)
    bot.tree.add_command(cog.tokens)
    bot.tree.add_command(cog.engagement)
    bot.tree.add_command(cog.leaderboard)
