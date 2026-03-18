from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.engagement import EngagementRepository
from repositories.prize_tokens import PrizeTokensRepository
from repositories.happy_jump_dollars import HappyJumpDollarRepository
from repositories.free_raffle_repo import FreeRaffleRepository
from services.engagement_service import EngagementService, level_from_total_xp, required_total_xp
from services.happy_jump_dollar_service import HappyJumpDollarService
from services.prize_token_service import PrizeTokenService
from services.role_reward_service import RoleRewardService
from utils.database import get_pool


def _message_has_interaction_origin(message: discord.Message) -> bool:
    return getattr(message, "interaction_metadata", None) is not None


class EngagementCog(commands.Cog):
    profile = app_commands.Group(name="profile", description="View engagement profiles")
    tokens = app_commands.Group(name="tokens", description="Prize token commands")
    leaderboard = app_commands.Group(name="leaderboard", description="Engagement leaderboards")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        pool = get_pool()
        self.repo = EngagementRepository(pool)
        self.token_repo = PrizeTokensRepository(pool)
        self.hjd_repo = HappyJumpDollarRepository(pool)
        self.token_service = PrizeTokenService(self.token_repo)
        self.hjd_service = HappyJumpDollarService(self.hjd_repo)
        self.service = EngagementService(self.repo, self.token_service, self.hjd_service)
        self.role_rewards = RoleRewardService(self.repo)
        self.voice_xp_worker.start()
        self.role_repair_worker.start()

    def cog_unload(self):
        self.voice_xp_worker.cancel()
        self.role_repair_worker.cancel()

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
            await channel.send(
                f"🎉 {member.mention} reached Level {level} and earned 1 coin and 100 HJD."
            )
        except Exception:
            return

    async def _sync_roles_for_member(self, guild_id: int, user_id: int) -> dict:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"granted": 0, "removed": 0, "failed": 1}
        member = guild.get_member(user_id)
        if member is None:
            return {"granted": 0, "removed": 0, "failed": 1}
        profile = await self.repo.get_or_create_profile(guild_id, user_id)
        return await self.role_rewards.sync_member_roles(guild, member, profile)

    async def _process_message_auto_entries(self, guild_id: int, user_id: int) -> int:
        settings = await self.repo.get_or_create_guild_settings(guild_id)
        if not bool(settings.get("auto_entry_giveaways_enabled", True)):
            return 0
        raffle_repo = FreeRaffleRepository(get_pool())
        giveaways = await raffle_repo.list_active_auto_entry_raffles(guild_id)
        if not giveaways:
            return 0
        profile = await self.repo.get_or_create_profile(guild_id, user_id)
        if int(profile.get("prize_token_balance") or 0) < 1:
            return 0
        weight = self.role_rewards.giveaway_weight_for_level(int(profile.get("level") or 0))
        awarded = 0
        bucket = int(datetime.now(timezone.utc).timestamp())
        for giveaway in giveaways:
            result = await raffle_repo.increment_auto_entry_progress(
                guild_id=guild_id,
                raffle_id=int(giveaway["id"]),
                user_id=user_id,
                entry_weight=weight,
                progress_dedupe_key=f"giveaway_auto_entry:{int(giveaway['id'])}:{user_id}:{bucket}",
            )
            if result.get("awarded"):
                awarded += int(result.get("entries_granted") or 0)
        return awarded

    @tasks.loop(minutes=10)
    async def role_repair_worker(self):
        for guild in list(self.bot.guilds):
            try:
                await self.role_rewards.seed_default_ladders_if_missing(guild.id)
                await self.role_rewards.ensure_reward_roles(guild)
                profiles = await self.repo.list_profiles_for_guild(guild.id)
                for p in profiles:
                    await self._sync_roles_for_member(guild.id, int(p["user_id"]))
            except Exception:
                continue

    @role_repair_worker.before_loop
    async def before_role_repair_worker(self):
        await self.bot.wait_until_ready()

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
                        if bool(getattr(voice_state, "self_mute", False)) and bool(
                            getattr(voice_state, "self_deaf", False)
                        ):
                            continue
                        await self.service.voice_xp_if_eligible(
                            guild_id=guild.id,
                            user_id=member.id,
                            channel_id=channel.id,
                            role_ids=[r.id for r in getattr(member, "roles", [])],
                            category_id=getattr(getattr(channel, "category", None), "id", None),
                            minute_bucket=minute_bucket,
                            on_level_up=self._post_levelup_announcement,
                            on_role_sync_needed=self._sync_roles_for_member,
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
        if _message_has_interaction_origin(message) or message.content.strip().startswith("/"):
            return
        applied = await self.service.message_xp_if_eligible(
            guild_id=message.guild.id,
            user_id=message.author.id,
            content=message.content,
            channel_id=message.channel.id,
            role_ids=[r.id for r in getattr(message.author, "roles", [])],
            category_id=getattr(getattr(message.channel, "category", None), "id", None),
            on_level_up=self._post_levelup_announcement,
            on_role_sync_needed=self._sync_roles_for_member,
        )
        if applied:
            await self._process_message_auto_entries(message.guild.id, message.author.id)

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
            on_role_sync_needed=self._sync_roles_for_member,
        )

    @commands.Cog.listener()
    async def on_paid_raffle_purchase_verified(self, payload: dict):
        await self.service.process_paid_raffle_purchase(
            payload, on_role_sync_needed=self._sync_roles_for_member
        )

    @commands.Cog.listener()
    async def on_raffle_prize_token_purchase_confirmed(self, payload: dict):
        await self.service.process_raffle_prize_token_purchase_confirmed(
            payload, on_role_sync_needed=self._sync_roles_for_member
        )

    @commands.Cog.listener()
    async def on_jump_99k_purchase_verified(self, payload: dict):
        await self.service.process_jump_purchase_verified(
            payload, on_role_sync_needed=self._sync_roles_for_member
        )

    @commands.Cog.listener()
    async def on_jump_99k_completed(self, payload: dict):
        await self.service.process_jump_completed(
            payload, on_role_sync_needed=self._sync_roles_for_member
        )

    @commands.Cog.listener()
    async def on_giveaway_joined(self, _payload: dict):
        return

    @commands.Cog.listener()
    async def on_prize_token_transaction_applied(self, payload: dict):
        guild_id = int(payload.get("guild_id") or 0)
        user_id = int(payload.get("user_id") or 0)
        if guild_id and user_id:
            await self._sync_roles_for_member(guild_id, user_id)

    @commands.Cog.listener()
    async def on_giveaway_started(self, payload: dict):
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
            f"Coins: **{int(p.get('prize_token_balance') or 0)}**",
            f"Coin Lifetime Earned/Spent: **{int(p.get('prize_token_lifetime_earned') or 0)} / {int(p.get('prize_token_lifetime_spent') or 0)}**",
            f"HJD Balance: **{int(p.get('hjd_balance') or 0)}**",
            f"HJD Lifetime Earned/Spent: **{int(p.get('hjd_lifetime_earned') or 0)} / {int(p.get('hjd_lifetime_spent') or 0)}**",
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
            f"Rank #{rank} • L{level} • `{bar}` {pct}%\nXP: {xp_total} • Coins: {int(p.get('prize_token_balance') or 0)} • HJD: {int(p.get('hjd_balance') or 0)}",
            ephemeral=True,
        )

    @profile.command(name="rewards", description="View rewards progress")
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
        level_role, activity_roles = await self.role_rewards.describe_member_rewards(interaction.guild, target, p)
        activity_text = ", ".join(activity_roles) if activity_roles else "None yet"
        await interaction.response.send_message(
            f"Level: **{level}**\n"
            f"XP Total: **{xp_total}**\n"
            f"Coin Balance: **{int(p.get('prize_token_balance') or 0)}**\n"
            f"HJD Balance: **{int(p.get('hjd_balance') or 0)}**\n"
            f"XP needed for next level: **{left}**\n"
            f"Current level role: **{level_role or 'None yet'}**\n"
            f"Earned activity/supporter roles: **{activity_text}**",
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
        txs = await self.token_repo.get_recent_transactions(
            interaction.guild.id, target.id, limit=10
        )
        if not txs:
            await interaction.response.send_message("No token history yet.", ephemeral=True)
            return
        lines = []
        for tx in txs:
            lines.append(
                f"`{tx.get('id')}` {tx.get('transaction_type')}: {int(tx.get('amount') or 0):+d} → {int(tx.get('balance_after') or 0)}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _send_leaderboard(
        self, interaction: discord.Interaction, board: str, title: str, value_key: str
    ):
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
        await self._send_leaderboard(
            interaction, "tokens", "Tokens Leaderboard", "prize_token_lifetime_earned"
        )

    @leaderboard.command(name="jumps", description="Top users by completed jumps")
    async def leaderboard_jumps(self, interaction: discord.Interaction):
        await self._send_leaderboard(
            interaction, "jumps", "Jumps Leaderboard", "jump_99k_completed_count"
        )

    @leaderboard.command(name="raffles", description="Top users by paid raffle tickets")
    async def leaderboard_raffles(self, interaction: discord.Interaction):
        await self._send_leaderboard(
            interaction, "raffles", "Raffles Leaderboard", "paid_raffle_tickets_count"
        )






async def setup(bot: commands.Bot):
    await bot.add_cog(EngagementCog(bot))
