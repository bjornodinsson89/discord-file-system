import asyncio
from types import SimpleNamespace

from setup_panel import RolesView


def test_roles_view_component_limits_and_no_insurer_profile_button():
    async def _run():
        view = RolesView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=SimpleNamespace(),
            panel=SimpleNamespace(),
        )

        assert len(view.children) <= 25

        row_indices = {child.row if child.row is not None else 0 for child in view.children}
        assert len(row_indices) <= 5

        labels = {getattr(child, "label", None) for child in view.children}
        assert "Edit my insurer profile" not in labels
        assert "Back" in labels

    asyncio.run(_run())
