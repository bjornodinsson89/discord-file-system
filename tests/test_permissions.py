from types import SimpleNamespace

from setup_panel import _missing_channel_perms, has_setup_permission


def test_has_setup_permission_owner():
    assert has_setup_permission(1, 1, False, False, set(), [])


def test_has_setup_permission_administrator():
    assert has_setup_permission(2, 1, True, False, set(), [])


def test_has_setup_permission_manage_guild():
    assert has_setup_permission(2, 1, False, True, set(), [])


def test_has_setup_permission_admin_role():
    assert has_setup_permission(2, 1, False, False, {"99"}, ["99"])


def test_has_setup_permission_denied():
    assert not has_setup_permission(2, 1, False, False, {"10"}, ["99"])


class _Channel:
    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=False, send_messages=True, embed_links=False)


def test_missing_channel_permissions_helper():
    missing = _missing_channel_perms(_Channel(), object())
    assert missing == ["View Channel", "Embed Links"]
