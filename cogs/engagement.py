from __future__ import annotations

from datetime import UTC, datetime

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
        self.service.set_level_up_callback(self._announce_level_up)
        self.voice_xp_worker.start()

    def cog_unload(self) -> None:
        self.voice_xp_worker.cancel()

    async def _announce_level_up(self, guild_id: int, user_id: int, level: int) -> None:
        settings = await self.repo.get_or_create_guild_settings(guild_id)
        channel_id = settings.get("levelup_channel_id")
        if not channel_id:
            return
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(
                f"🎉 <@{int(user_id)}> reached Level {int(level)} and earned 1 Prize Token."
            )
        except Exception:
            return

    async def _profile_for(self, guild_id: int, user_id: int) -> dict:
        return await self.repo.get_or_create_profile(guild_id, user_id)

    async def _render_leaderboard(
        self, interaction: discord.Interaction, title: str, rows: list[dict], value_key: str
    ) -> None:
        lines: list[str] = []
        for idx, row in enumerate(rows, start=1):
            lines.append(
                f"{idx}. <@{int(row['user_id'])}> — **{int(row.get(value_key) or 0)}**"
            )
        if not lines:
            lines.append("No data yet.")
        await interaction.response.send_message(f"**{title}**\n" + "\n".join(lines), ephemeral=True)

    @tasks.loop(seconds=60)
    async def voice_xp_worker(self):
        minute_bucket = int(datetime.now(UTC).timestamp()) // 60
        for guild in self.bot.guilds:
            settings = await self.repo.get_or_create_guild_settings(guild.id)
            if not settings.get("enabled") or not settings.get("voice_xp_enabled"):
                continue
            afk_id = getattr(guild, "afk_channel", None)
            afk_channel_id = getattr(afk_id, "id", None)
            ignored_categories = set(settings.get("ignored_category_ids_json") or [])
            for channel in guild.voice_channels:
                if channel.id == afk_channel_id:
                    continue
                if channel.category_id and channel.category_id in ignored_categories:
                    continue
                humans = [m for m in channel.members if not m.bot]
                if len(humans) < 2:
                    continue
                for member in humans:
                    vs = member.voice
                    if vs and vs.self_mute and vs.self_deaf:
                        continue
                    try:
                        await self.service.voice_xp_if_eligible(
                            guild_id=guild.id,
                            user_id=member.id,
                            channel_id=channel.id,
                            minute_bucket=minute_bucket,
                        )
                    except Exception:
                        continue

    @voice_xp_worker.before_loop
    async def before_voice_worker(self):
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
    async def profile_view(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
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
    async def profile_rank(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
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

    @profile.command(name="rewards", description="View reward role status")
    async def profile_rewards(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
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
            "\n".join(
                [
                    f"Level: **{level}**",
                    f"XP Total: **{xp_total}**",
                    f"Prize Token balance: **{int(p.get('prize_token_balance') or 0)}**",
                    f"XP needed for next level: **{left}**",
                    "Reward roles are not configured yet.",
                ]
            ),
            ephemeral=True,
        )

    @tokens.command(name="balance", description="View token balance")
    async def tokens_balance(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
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
    async def tokens_history(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = member or interaction.user
        rows = await self.token_repo.get_recent_transactions(interaction.guild.id, target.id, limit=10)
        if not rows:
            await interaction.response.send_message("No token history found.", ephemeral=True)
            return
        lines = [
            f"{r['id']}: {r['transaction_type']} {r['amount']} (balance {r['balance_after']})"
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @leaderboard.command(name="xp", description="Top users by XP")
    async def leaderboard_xp(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.repo.leaderboard_xp(interaction.guild.id)
        await self._render_leaderboard(interaction, "Leaderboard: XP", rows, "xp_total")

    @leaderboard.command(name="levels", description="Top users by level")
    async def leaderboard_levels(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.repo.leaderboard_levels(interaction.guild.id)
        await self._render_leaderboard(interaction, "Leaderboard: Levels", rows, "level")

    @leaderboard.command(name="tokens", description="Top users by lifetime token earnings")
    async def leaderboard_tokens(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.repo.leaderboard_tokens(interaction.guild.id)
        await self._render_leaderboard(
            interaction, "Leaderboard: Tokens", rows, "prize_token_lifetime_earned"
        )

    @leaderboard.command(name="jumps", description="Top users by 99k completions")
    async def leaderboard_jumps(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.repo.leaderboard_jumps(interaction.guild.id)
        await self._render_leaderboard(
            interaction, "Leaderboard: Jumps", rows, "jump_99k_completed_count"
        )

    @leaderboard.command(name="raffles", description="Top users by paid raffle tickets")
    async def leaderboard_raffles(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.repo.leaderboard_raffles(interaction.guild.id)
        await self._render_leaderboard(
            interaction, "Leaderboard: Raffles", rows, "paid_raffle_tickets_count"
        )

    @engagement.command(name="debug", description="Debug engagement state")
    @app_commands.checks.has_permissions(administrator=True)
    async def engagement_debug(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
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

    @engagement.command(name="config", description="View engagement configuration")
    @app_commands.checks.has_permissions(administrator=True)
    async def engagement_config(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        settings = await self.repo.get_or_create_guild_settings(interaction.guild.id)
        lines = [
            f"enabled: `{bool(settings.get('enabled'))}`",
            f"levelup_channel_id: `{settings.get('levelup_channel_id')}`",
            f"leaderboard_enabled: `{bool(settings.get('leaderboard_enabled'))}`",
            f"profile_cards_enabled: `{bool(settings.get('profile_cards_enabled'))}`",
            f"message_xp_enabled: `{bool(settings.get('message_xp_enabled'))}`",
            f"reaction_xp_enabled: `{bool(settings.get('reaction_xp_enabled'))}`",
            f"voice_xp_enabled: `{bool(settings.get('voice_xp_enabled'))}`",
            f"message_xp_amount: `{int(settings.get('message_xp_amount') or 0)}`",
            f"message_xp_cooldown_seconds: `{int(settings.get('message_xp_cooldown_seconds') or 0)}`",
            f"reaction_xp_amount: `{int(settings.get('reaction_xp_amount') or 0)}`",
            f"reaction_xp_hourly_cap: `{int(settings.get('reaction_xp_hourly_cap') or 0)}`",
            f"voice_xp_per_minute: `{int(settings.get('voice_xp_per_minute') or 0)}`",
            f"paid_raffle_purchase_xp_base/per_ticket/cap: `{int(settings.get('paid_raffle_purchase_xp_base') or 0)}/{int(settings.get('paid_raffle_purchase_xp_per_ticket') or 0)}/{int(settings.get('paid_raffle_purchase_xp_cap') or 0)}`",
            f"jump_purchase_xp: `{int(settings.get('jump_purchase_xp') or 0)}`",
            f"jump_completion_xp: `{int(settings.get('jump_completion_xp') or 0)}`",
            f"auto_entry_giveaways_enabled: `{bool(settings.get('auto_entry_giveaways_enabled'))}`",
            f"ignored_channel_ids_json: `{settings.get('ignored_channel_ids_json')}`",
            f"ignored_category_ids_json: `{settings.get('ignored_category_ids_json')}`",
            f"ignored_role_ids_json: `{settings.get('ignored_role_ids_json')}`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    cog = EngagementCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.profile)
    bot.tree.add_command(cog.tokens)
    bot.tree.add_command(cog.engagement)
    bot.tree.add_command(cog.leaderboard)
