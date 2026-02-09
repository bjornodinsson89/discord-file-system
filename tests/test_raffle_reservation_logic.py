from utils.database import DatabaseManager


def test_unpaid_reservation_overwrites_ticket_count():
    assert DatabaseManager.merge_raffle_tickets(5, 2, payment_verified=False) == 2


def test_paid_tickets_accumulate():
    assert DatabaseManager.merge_raffle_tickets(5, 2, payment_verified=True) == 7
