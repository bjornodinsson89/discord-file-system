def merge_raffle_tickets(
    existing_tickets: int, incoming_tickets: int, payment_verified: bool
) -> int:
    if payment_verified:
        return int(existing_tickets) + int(incoming_tickets)
    return int(incoming_tickets)


def test_unpaid_reservation_overwrites_ticket_count():
    assert merge_raffle_tickets(5, 2, payment_verified=False) == 2


def test_paid_tickets_accumulate():
    assert merge_raffle_tickets(5, 2, payment_verified=True) == 7
