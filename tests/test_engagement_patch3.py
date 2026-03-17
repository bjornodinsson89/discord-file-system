from __future__ import annotations

import asyncio
from pathlib import Path

from services.role_reward_service import RoleRewardService


class _FakeRole:
    def __init__(self, role_id: int):
        self.id = role_id


class _FakeMember:
    def __init__(self, member_id: int, roles=None, fail_add=False, fail_remove=False):
        self.id = member_id
        self.roles = list(roles or [])
        self._fail_add = fail_add
        self._fail_remove = fail_remove

    async def add_roles(self, role, reason: str | None = None):
        if self._fail_add:
            raise RuntimeError("no permission")
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, reason: str | None = None):
        if self._fail_remove:
            raise RuntimeError("no permission")
        if role in self.roles:
            self.roles.remove(role)


class _FakeGuild:
    def __init__(self, guild_id: int, roles):
        self.id = guild_id
        self._roles = {r.id: r for r in roles}

    def get_role(self, rid: int):
        return self._roles.get(rid)


class _FakeRepo:
    def __init__(self):
        self.seeded = False

    async def seed_default_reward_ladders(self, _guild_id: int):
        self.seeded = True

    async def list_level_role_rewards(self, _guild_id: int):
        return [
            {"level_required": 5, "role_id": 100, "remove_lower_tiers": True},
            {"level_required": 10, "role_id": 101, "remove_lower_tiers": True},
            {"level_required": 20, "role_id": 102, "remove_lower_tiers": True},
        ]

    async def list_prize_roles(self, _guild_id: int):
        return [
            {"milestone_type": "lifetime_tokens_earned", "milestone_value": 5, "role_id": 201},
            {"milestone_type": "jump_completions", "milestone_value": 5, "role_id": 202},
            {"milestone_type": "raffle_purchases", "milestone_value": 10, "role_id": 203},
        ]


def test_level_bands_weight_exact_values():
    service = RoleRewardService(_FakeRepo())
    assert service.giveaway_weight_for_level(1) == 1
    assert service.giveaway_weight_for_level(9) == 1
    assert service.giveaway_weight_for_level(10) == 2
    assert service.giveaway_weight_for_level(24) == 2
    assert service.giveaway_weight_for_level(25) == 3
    assert service.giveaway_weight_for_level(49) == 3
    assert service.giveaway_weight_for_level(50) == 4


def test_prize_roles_stack_and_level_role_exclusive_highest_tier_only():
    async def _run():
        repo = _FakeRepo()
        service = RoleRewardService(repo)
        guild = _FakeGuild(
            1,
            [
                _FakeRole(100),
                _FakeRole(101),
                _FakeRole(102),
                _FakeRole(201),
                _FakeRole(202),
                _FakeRole(203),
            ],
        )
        member = _FakeMember(9, roles=[guild.get_role(100), guild.get_role(101)])

        profile = {
            "level": 22,
            "prize_token_lifetime_earned": 10,
            "jump_99k_completed_count": 6,
            "paid_raffle_purchases_count": 11,
        }
        result = await service.sync_member_roles(guild, member, profile)
        assert guild.get_role(102) in member.roles
        assert guild.get_role(100) not in member.roles
        assert guild.get_role(101) not in member.roles
        assert guild.get_role(201) in member.roles
        assert guild.get_role(202) in member.roles
        assert guild.get_role(203) in member.roles
        assert result["granted"] >= 4

    asyncio.run(_run())


def test_role_sync_fails_soft_missing_role_or_permissions():
    async def _run():
        repo = _FakeRepo()
        service = RoleRewardService(repo)
        guild = _FakeGuild(1, [_FakeRole(100)])
        member = _FakeMember(9, fail_add=True)
        profile = {
            "level": 30,
            "prize_token_lifetime_earned": 100,
            "jump_99k_completed_count": 100,
            "paid_raffle_purchases_count": 100,
        }
        result = await service.sync_member_roles(guild, member, profile)
        assert result["failed"] >= 1

    asyncio.run(_run())


def test_patch3_sources_include_commands_workers_and_setup_page4_roles():
    engagement_src = Path("cogs/engagement.py").read_text(encoding="utf-8")
    for expected in [
        '@engagement.command(name="sync_roles"',
        '@engagement.command(name="reverse_event"',
        '@engagement.command(name="rebuild_profile"',
        "auto_entry_reconciliation_worker",
        "role_repair_worker",
        "giveaway_auto_entry:{giveaway_id}:{user_id}",
    ]:
        assert expected in engagement_src

    setup_src = Path("setup_panel.py").read_text(encoding="utf-8")
    for expected in [
        "class EngagementRolesView",
        "Manage level roles",
        "Manage prize roles",
        "Sync all reward roles",
        "Seed default reward ladder",
    ]:
        assert expected in setup_src


def test_migration_has_patch3_tables_and_entry_fields():
    src = Path("migrations/2026_03_18_engagement_patch3_roles_and_giveaway.sql").read_text(
        encoding="utf-8"
    )
    assert "engagement_role_rewards" in src
    assert "engagement_prize_roles" in src
    assert "entry_source" in src
    assert "entry_weight" in src
    assert "dedupe_key" in src


def test_weighted_draw_path_uses_entry_weight_without_row_duplication():
    src = Path("repositories/free_raffle_repo.py").read_text(encoding="utf-8")
    assert "weighted_odds_enabled" in src
    assert "entry_weight" in src
    assert "secrets.randbelow" in src
    assert "INSERT INTO free_raffle_entries" in src
