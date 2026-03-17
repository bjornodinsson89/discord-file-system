from __future__ import annotations

import logging

import discord

from services.engagement_service import level_from_total_xp

log = logging.getLogger("happy_jumper.role_rewards")


LEVEL_ROLE_DEFINITIONS = [
    (1, "Pickpocket", "95A5A6"),
    (4, "Mugger", "7F8C8D"),
    (7, "Booster", "8E44AD"),
    (10, "Wheelman", "9B59B6"),
    (13, "Safecracker", "2980B9"),
    (16, "Drug Runner", "3498DB"),
    (19, "Racketeer", "16A085"),
    (22, "Loan Shark", "1ABC9C"),
    (25, "Bookie", "27AE60"),
    (28, "Arms Dealer", "2ECC71"),
    (31, "Heist Planner", "F39C12"),
    (34, "Faction Soldier", "F1C40F"),
    (37, "Street Boss", "D35400"),
    (40, "Underboss", "E67E22"),
    (43, "Crime Lord", "C0392B"),
    (46, "Faction Heavy", "E74C3C"),
    (49, "Torn Legend", "FF4D6D"),
    (50, "City Kingpin", "FD79A8"),
]

MILESTONE_ROLE_DEFINITIONS = [
    ("lifetime_tokens_earned", 5, "Supporter", "00CEC9"),
    ("lifetime_tokens_earned", 15, "High Roller", "00B894"),
    ("lifetime_tokens_earned", 30, "Whale", "55EFC4"),
    ("jump_completions", 3, "Jump Starter", "74B9FF"),
    ("jump_completions", 10, "Jump Specialist", "0984E3"),
    ("jump_completions", 25, "Airborne Addict", "6C5CE7"),
    ("raffle_purchases", 5, "Ticket Buyer", "FFEAA7"),
    ("raffle_purchases", 15, "Raffle Addict", "FDCB6E"),
    ("raffle_purchases", 30, "Jackpot Chaser", "E17055"),
    ("message_xp_total", 2500, "Talkative", "81ECEC"),
    ("message_xp_total", 10000, "Loudmouth", "00CEC9"),
    ("voice_xp_total", 1000, "Night Owl", "A29BFE"),
    ("voice_xp_total", 5000, "Radio Active", "6C5CE7"),
    ("reaction_xp_total", 500, "Crowd Favorite", "FAB1A0"),
    ("reaction_xp_total", 2000, "Local Celebrity", "FF7675"),
]


class RoleRewardService:
    def __init__(self, repo):
        self.repo = repo

    async def seed_default_ladders_if_missing(self, guild_id: int) -> None:
        await self.repo.seed_default_reward_ladders(guild_id)

    async def ensure_reward_roles(self, guild) -> tuple[int, int]:
        level_rewards = await self.repo.list_level_role_rewards(guild.id)
        prize_rewards = await self.repo.list_prize_roles(guild.id)
        created = repaired = 0
        for reward in [*level_rewards, *prize_rewards]:
            role_id = int(reward.get("role_id") or 0)
            role = guild.get_role(role_id) if role_id > 0 else None
            if role is not None:
                continue
            role_name = str(reward.get("role_name") or "Reward")
            try:
                color = discord.Colour(int(str(reward.get("role_color") or "000000"), 16))
            except ValueError:
                color = discord.Colour.default()

            try:
                new_role = await guild.create_role(
                    name=role_name,
                    colour=color,
                    permissions=discord.Permissions.none(),
                    mentionable=False,
                    hoist=False,
                    reason="Engagement reward role create/repair",
                )
            except Exception as exc:
                log.warning("reward role create/repair failed guild_id=%s role=%s err=%s", guild.id, role_name, exc)
                continue
            if reward.get("level_required") is not None:
                await self.repo.set_level_reward_role_id(guild.id, int(reward["level_required"]), int(new_role.id))
            else:
                await self.repo.set_prize_reward_role_id(
                    guild.id,
                    str(reward.get("milestone_type") or ""),
                    int(reward.get("milestone_value") or 0),
                    int(new_role.id),
                )
            if role_id > 0:
                repaired += 1
            else:
                created += 1
        return created, repaired

    def giveaway_weight_for_level(self, level: int) -> int:
        if level >= 50:
            return 4
        if level >= 25:
            return 3
        if level >= 10:
            return 2
        return 1

    def prize_role_eligible(self, profile: dict, milestone_type: str, milestone_value: int) -> bool:
        mapping = {
            "lifetime_tokens_earned": "prize_token_lifetime_earned",
            "jump_completions": "jump_99k_completed_count",
            "raffle_purchases": "paid_raffle_purchases_count",
            "message_xp_total": "message_xp_total",
            "voice_xp_total": "voice_xp_total",
            "reaction_xp_total": "reaction_xp_total",
        }
        field = mapping.get(milestone_type)
        return int(profile.get(field) or 0) >= milestone_value if field else False

    async def sync_member_roles(self, guild, member, profile: dict | None = None) -> dict:
        profile = profile or await self.repo.get_or_create_profile(guild.id, member.id)
        await self.seed_default_ladders_if_missing(guild.id)
        await self.ensure_reward_roles(guild)
        level_rewards = await self.repo.list_level_role_rewards(guild.id)
        prize_rewards = await self.repo.list_prize_roles(guild.id)

        granted = removed = failed = 0
        eligible_level = [r for r in level_rewards if int(profile.get("level") or 0) >= int(r.get("level_required") or 0)]
        target_level_reward = eligible_level[-1] if eligible_level else None
        target_level_role_id = int(target_level_reward.get("role_id") or 0) if target_level_reward else 0

        if target_level_role_id > 0:
            target_role = guild.get_role(target_level_role_id)
            if target_role is None:
                failed += 1
            elif target_role not in member.roles:
                try:
                    await member.add_roles(target_role, reason="Engagement level reward sync")
                    granted += 1
                except Exception as exc:
                    failed += 1
                    log.warning("add level role failed guild_id=%s user_id=%s err=%s", guild.id, member.id, exc)

        for reward in level_rewards:
            role_id = int(reward.get("role_id") or 0)
            if role_id <= 0 or role_id == target_level_role_id:
                continue
            role = guild.get_role(role_id)
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Engagement level reward tier cleanup")
                    removed += 1
                except Exception as exc:
                    failed += 1
                    log.warning("remove level role failed guild_id=%s user_id=%s err=%s", guild.id, member.id, exc)

        for reward in prize_rewards:
            role_id = int(reward.get("role_id") or 0)
            if role_id <= 0 or not self.prize_role_eligible(profile, str(reward.get("milestone_type") or ""), int(reward.get("milestone_value") or 0)):
                continue
            role = guild.get_role(role_id)
            if role is None or role in member.roles:
                continue
            try:
                await member.add_roles(role, reason="Engagement milestone reward sync")
                granted += 1
            except Exception as exc:
                failed += 1
                log.warning("add milestone role failed guild_id=%s user_id=%s err=%s", guild.id, member.id, exc)

        return {"granted": granted, "removed": removed, "failed": failed}

    async def describe_member_rewards(self, guild, member, profile: dict) -> tuple[str | None, list[str]]:
        await self.seed_default_ladders_if_missing(guild.id)
        await self.ensure_reward_roles(guild)
        level_rewards = await self.repo.list_level_role_rewards(guild.id)
        prize_rewards = await self.repo.list_prize_roles(guild.id)
        level_name = None
        for reward in level_rewards:
            if int(profile.get("level") or 0) >= int(reward.get("level_required") or 0):
                level_name = str(reward.get("role_name") or "")
        activity = [
            str(r.get("role_name") or "")
            for r in prize_rewards
            if self.prize_role_eligible(profile, str(r.get("milestone_type") or ""), int(r.get("milestone_value") or 0))
        ]
        return level_name, activity

    async def rewards_status(self, guild_id: int, guild) -> dict:
        await self.seed_default_ladders_if_missing(guild_id)
        level_rewards = await self.repo.list_level_role_rewards(guild_id)
        prize_rewards = await self.repo.list_prize_roles(guild_id)
        rows = [*level_rewards, *prize_rewards]
        linked = sum(1 for r in rows if int(r.get("role_id") or 0) > 0 and guild.get_role(int(r.get("role_id") or 0)) is not None)
        missing = len(rows) - linked
        return {"total": len(rows), "linked": linked, "missing": missing}

    async def rebuild_profile_level(self, guild_id: int, user_id: int) -> int:
        profile = await self.repo.get_or_create_profile(guild_id, user_id)
        level = level_from_total_xp(int(profile.get("xp_total") or 0))
        await self.repo.update_level(guild_id, user_id, level)
        return level
