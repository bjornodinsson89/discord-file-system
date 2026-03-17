from __future__ import annotations

import asyncio
from pathlib import Path

from services.role_reward_service import RoleRewardService


class _FakeRole:
    def __init__(self, role_id: int, name: str = "", color: int = 0):
        self.id = role_id
        self.name = name
        self.color = color


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
        self.created_roles = []

    def get_role(self, rid: int):
        return self._roles.get(rid)

    async def create_role(self, *, name, colour, permissions, mentionable, hoist, reason):
        rid = max(self._roles.keys(), default=1000) + 1
        role = _FakeRole(rid, name=name, color=getattr(colour, "value", 0))
        role.permissions = permissions
        role.mentionable = mentionable
        role.hoist = hoist
        self._roles[rid] = role
        self.created_roles.append(role)
        return role


class _FakeRepo:
    def __init__(self):
        self.seeded = False
        self.level_rewards = [
            {
                "level_required": 1,
                "role_id": 100,
                "role_name": "Pickpocket",
                "role_color": "95A5A6",
            },
            {
                "level_required": 50,
                "role_id": 101,
                "role_name": "City Kingpin",
                "role_color": "FD79A8",
            },
        ]
        self.prize_rewards = [
            {
                "milestone_type": "lifetime_tokens_earned",
                "milestone_value": 5,
                "role_id": 201,
                "role_name": "Supporter",
                "role_color": "00CEC9",
            },
            {
                "milestone_type": "jump_completions",
                "milestone_value": 3,
                "role_id": 202,
                "role_name": "Jump Starter",
                "role_color": "74B9FF",
            },
        ]

    async def seed_default_reward_ladders(self, _guild_id: int):
        self.seeded = True

    async def list_level_role_rewards(self, _guild_id: int):
        return self.level_rewards

    async def list_prize_roles(self, _guild_id: int):
        return self.prize_rewards

    async def set_level_reward_role_id(self, _guild_id: int, level_required: int, role_id: int):
        for row in self.level_rewards:
            if row["level_required"] == level_required:
                row["role_id"] = role_id

    async def set_prize_reward_role_id(
        self, _guild_id: int, milestone_type: str, milestone_value: int, role_id: int
    ):
        for row in self.prize_rewards:
            if (
                row["milestone_type"] == milestone_type
                and row["milestone_value"] == milestone_value
            ):
                row["role_id"] = role_id


def test_level_bands_weight_exact_values():
    service = RoleRewardService(_FakeRepo())
    assert service.giveaway_weight_for_level(1) == 1
    assert service.giveaway_weight_for_level(10) == 2
    assert service.giveaway_weight_for_level(25) == 3
    assert service.giveaway_weight_for_level(50) == 4


def test_role_sync_keeps_highest_level_and_stacks_activity_roles():
    async def _run():
        repo = _FakeRepo()
        repo.level_rewards = [
            {
                "level_required": 1,
                "role_id": 100,
                "role_name": "Pickpocket",
                "role_color": "95A5A6",
            },
            {"level_required": 4, "role_id": 101, "role_name": "Mugger", "role_color": "7F8C8D"},
            {
                "level_required": 50,
                "role_id": 102,
                "role_name": "City Kingpin",
                "role_color": "FD79A8",
            },
        ]
        repo.prize_rewards = [
            {
                "milestone_type": "lifetime_tokens_earned",
                "milestone_value": 5,
                "role_id": 201,
                "role_name": "Supporter",
                "role_color": "00CEC9",
            },
            {
                "milestone_type": "jump_completions",
                "milestone_value": 3,
                "role_id": 202,
                "role_name": "Jump Starter",
                "role_color": "74B9FF",
            },
        ]
        service = RoleRewardService(repo)
        guild = _FakeGuild(
            1, [_FakeRole(100), _FakeRole(101), _FakeRole(102), _FakeRole(201), _FakeRole(202)]
        )
        member = _FakeMember(9, roles=[guild.get_role(100), guild.get_role(101)])

        result = await service.sync_member_roles(
            guild,
            member,
            {"level": 80, "prize_token_lifetime_earned": 10, "jump_99k_completed_count": 4},
        )
        assert guild.get_role(102) in member.roles
        assert guild.get_role(100) not in member.roles
        assert guild.get_role(101) not in member.roles
        assert guild.get_role(201) in member.roles
        assert guild.get_role(202) in member.roles
        assert result["failed"] == 0

    asyncio.run(_run())


def test_missing_or_deleted_role_is_created_and_relinked():
    async def _run():
        repo = _FakeRepo()
        repo.level_rewards[0]["role_id"] = 0
        repo.prize_rewards[0]["role_id"] = 99999
        service = RoleRewardService(repo)
        guild = _FakeGuild(1, [_FakeRole(100), _FakeRole(201)])
        created, repaired = await service.ensure_reward_roles(guild)
        assert created >= 1
        assert repaired >= 1
        assert any(r.name == "Pickpocket" for r in guild.created_roles)
        assert all(r.mentionable is False for r in guild.created_roles)
        assert all(r.hoist is False for r in guild.created_roles)

    asyncio.run(_run())


def test_profile_rewards_command_source_mentions_current_role_state():
    src = Path("cogs/engagement.py").read_text(encoding="utf-8")
    assert "Current level role" in src
    assert "Earned activity/supporter roles" in src


def test_patch3_sources_include_setup_role_actions_and_migration_metadata_columns():
    setup_src = Path("setup_panel.py").read_text(encoding="utf-8")
    for expected in [
        "Create/Repair Reward Roles",
        "Sync All Reward Roles",
        "Reseed Reward Role Definitions",
        "View Reward Role Status",
    ]:
        assert expected in setup_src

    migration_src = Path("migrations/2026_03_20_engagement_reward_role_metadata.sql").read_text(
        encoding="utf-8"
    )
    assert "role_name" in migration_src
    assert "role_color" in migration_src
    assert "auto_created" in migration_src


def test_locked_seed_definitions_have_expected_counts_and_thresholds():
    from services.role_reward_service import LEVEL_ROLE_DEFINITIONS, MILESTONE_ROLE_DEFINITIONS

    assert len(LEVEL_ROLE_DEFINITIONS) == 18
    assert [level for level, _, _ in LEVEL_ROLE_DEFINITIONS] == [
        1,
        4,
        7,
        10,
        13,
        16,
        19,
        22,
        25,
        28,
        31,
        34,
        37,
        40,
        43,
        46,
        49,
        50,
    ]
    assert len(MILESTONE_ROLE_DEFINITIONS) == 15
    assert 5 not in [level for level, _, _ in LEVEL_ROLE_DEFINITIONS if level not in {1, 50}]
