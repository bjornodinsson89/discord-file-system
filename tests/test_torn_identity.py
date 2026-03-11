from types import SimpleNamespace

from services.torn_identity import parse_member_torn_identity_from_nickname


def test_parse_member_torn_identity_prefers_nick_then_display_name():
    member = SimpleNamespace(nick="Alice [1234567]", display_name="Bob [7654321]")
    torn_id, torn_name = parse_member_torn_identity_from_nickname(member)
    assert torn_id == 1234567
    assert torn_name == "Alice"


def test_parse_member_torn_identity_falls_back_to_display_name_when_nick_missing():
    member = SimpleNamespace(nick=None, display_name="Charlie [222333]")
    torn_id, torn_name = parse_member_torn_identity_from_nickname(member)
    assert torn_id == 222333
    assert torn_name == "Charlie"


def test_parse_member_torn_identity_rejects_invalid_format():
    member = SimpleNamespace(nick="No torn id", display_name="Still invalid")
    torn_id, torn_name = parse_member_torn_identity_from_nickname(member)
    assert torn_id is None
    assert torn_name is None
