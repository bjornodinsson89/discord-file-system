from setup_panel import has_setup_permission


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
