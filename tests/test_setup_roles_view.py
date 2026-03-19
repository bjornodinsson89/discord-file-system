import asyncio
from types import SimpleNamespace

from setup_panel import RewardRolesDashboardView, RolesDashboardView, SetupPanelView


def test_roles_dashboard_component_limits_and_navigation():
    async def _run():
        panel = SetupPanelView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=SimpleNamespace(),
        )
        panel.reward_role_status = {"missing": 0}
        view = RolesDashboardView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=SimpleNamespace(),
            panel=panel,
        )

        assert len(view.children) <= 25
        labels = {getattr(child, "label", None) for child in view.children}
        assert "Back" in labels
        assert "Home" in labels
        assert {"Admin Roles", "Host Role", "Insurance Role", "Reward Roles"}.issubset(labels)

    asyncio.run(_run())


def test_reward_roles_dashboard_stays_within_component_limits():
    async def _run():
        panel = SetupPanelView(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(),
        )
        view = RewardRolesDashboardView(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(),
            panel=panel,
        )
        assert len(view.children) <= 25
        rows = [child.row or 0 for child in view.children]
        assert max(rows) <= 4
        assert rows.count(0) <= 5
        assert rows.count(1) <= 5

    asyncio.run(_run())
