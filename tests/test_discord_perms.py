from types import SimpleNamespace

from utils.discord_perms import can_manage_paid_raffles, has_role


class _Role:
    def __init__(self, role_id: int):
        self.id = role_id


class _Member:
    def __init__(self, role_ids: list[int], *, administrator: bool = False):
        self.roles = [_Role(role_id) for role_id in role_ids]
        self.guild_permissions = SimpleNamespace(administrator=administrator)


def test_has_role_matches_member_roles() -> None:
    member = _Member([111, 222])
    assert has_role(member, 222)
    assert not has_role(member, 999)


def test_can_manage_paid_raffles_allows_discord_admin() -> None:
    member = _Member([], administrator=True)
    settings = {"raffle_host_role_id": None}
    assert can_manage_paid_raffles(member, settings)


def test_can_manage_paid_raffles_allows_configured_raffle_host_role() -> None:
    member = _Member([8002])
    settings = {"raffle_host_role_id": 8002}
    assert can_manage_paid_raffles(member, settings)


def test_can_manage_paid_raffles_denies_member_without_admin_or_host_role() -> None:
    member = _Member([9003])
    settings = {"raffle_host_role_id": 8002}
    assert not can_manage_paid_raffles(member, settings)
