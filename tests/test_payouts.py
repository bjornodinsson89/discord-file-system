from utils.payouts import parse_payout_string, payout_items_to_human, payout_items_to_string, PayoutParseError


def test_parse_merges_alias_duplicates():
    items = parse_payout_string("xanax=4, xans=2, edvd:6, erotic dvd=1, ecstasy=1")
    assert items == [
        {"item": "xanax", "qty": 6},
        {"item": "erotic_dvd", "qty": 7},
        {"item": "ecstasy", "qty": 1},
    ]


def test_parse_rejects_unknown_item():
    try:
        parse_payout_string("weed=2")
    except PayoutParseError as exc:
        assert "Unknown payout item" in str(exc)
    else:
        raise AssertionError("Expected PayoutParseError")


def test_string_and_human_helpers():
    items = [{"item": "xanax", "qty": 4}, {"item": "erotic_dvd", "qty": 6}, {"item": "ecstasy", "qty": 1}]
    assert payout_items_to_string(items) == "xanax=4, edvd=6, ecstasy=1"
    assert payout_items_to_human(items) == "4x Xanax • 6x eDVD • 1x Ecstasy"
