from __future__ import annotations

from services.engagement_service import level_from_total_xp


class RoleRewardService:
    def __init__(self, repo):
        self.repo = repo

    async def seed_default_ladders_if_missing(self, guild_id: int) -> None:
        await self.repo.seed_default_reward_ladders(guild_id)

    def giveaway_weight_for_level(self, level: int) -> int:
        if level >= 50:
            return 4
        if level >= 25:
            return 3
        if level >= 10:
            return 2
        return 1

    def prize_role_eligible(self, profile: dict, milestone_type: str, milestone_value: int) -> bool:
        if milestone_type == "lifetime_tokens_earned":
            return int(profile.get("prize_token_lifetime_earned") or 0) >= milestone_value
        if milestone_type == "jump_completions":
            return int(profile.get("jump_99k_completed_count") or 0) >= milestone_value
        if milestone_type == "raffle_purchases":
            return int(profile.get("paid_raffle_purchases_count") or 0) >= milestone_value
        return False

    async def sync_member_roles(self, guild, member, profile: dict | None = None) -> dict:
        profile = profile or await self.repo.get_or_create_profile(guild.id, member.id)
        level_rewards = await self.repo.list_level_role_rewards(guild.id)
        prize_rewards = await self.repo.list_prize_roles(guild.id)

        granted = 0
        removed = 0
        failed = 0

        eligible_level = [r for r in level_rewards if int(r.get("role_id") or 0) > 0 and int(profile.get("level") or 0) >= int(r.get("level_required") or 0)]
        target_level_reward = eligible_level[-1] if eligible_level else None

        if target_level_reward is not None:
            target_role = guild.get_role(int(target_level_reward["role_id"]))
            if target_role is None:
                failed += 1
            else:
                try:
                    if target_role not in member.roles:
                        await member.add_roles(target_role, reason="Engagement level reward sync")
                        granted += 1
                except Exception:
                    failed += 1

                if bool(target_level_reward.get("remove_lower_tiers", True)):
                    for reward in level_rewards:
                        role_id = int(reward.get("role_id") or 0)
                        if role_id <= 0 or role_id == int(target_level_reward["role_id"]):
                            continue
                        role = guild.get_role(role_id)
                        if role is not None and role in member.roles:
                            try:
                                await member.remove_roles(role, reason="Engagement level reward tier cleanup")
                                removed += 1
                            except Exception:
                                failed += 1

        for reward in prize_rewards:
            role_id = int(reward.get("role_id") or 0)
            if role_id <= 0:
                continue
            if not self.prize_role_eligible(profile, str(reward.get("milestone_type") or ""), int(reward.get("milestone_value") or 0)):
                continue
            role = guild.get_role(role_id)
            if role is None:
                failed += 1
                continue
            if role in member.roles:
                continue
            try:
                await member.add_roles(role, reason="Engagement prize milestone reward sync")
                granted += 1
            except Exception:
                failed += 1

        return {"granted": granted, "removed": removed, "failed": failed}

    async def rebuild_profile_level(self, guild_id: int, user_id: int) -> int:
        profile = await self.repo.get_or_create_profile(guild_id, user_id)
        level = level_from_total_xp(int(profile.get("xp_total") or 0))
        await self.repo.update_level(guild_id, user_id, level)
        return level
