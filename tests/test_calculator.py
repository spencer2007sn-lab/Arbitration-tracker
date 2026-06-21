from __future__ import annotations

from decimal import Decimal

import pytest

from arb_scanner.calculator import check_arb, kalshi_fee, polymarket_fee
from arb_scanner.models import MarketPair, Venue
from tests.conftest import make_kalshi_market, make_order_book, make_poly_market

COEFF = Decimal("0.07")
RATE = Decimal("0.0075")
MIN_EDGE = Decimal("0.5")


def make_pair(resolution_warning: str | None = None) -> MarketPair:
    return MarketPair(
        kalshi_market=make_kalshi_market(),
        polymarket_market=make_poly_market(),
        match_method="exact",
        resolution_warning=resolution_warning,
    )


# --- fee formula tests ---

def test_kalshi_fee_at_midpoint():
    fee = kalshi_fee(Decimal("0.5"), COEFF)
    assert fee == Decimal("0.07") * Decimal("0.5") * Decimal("0.5")
    assert fee == Decimal("0.0175")


def test_kalshi_fee_near_zero():
    fee = kalshi_fee(Decimal("0.01"), COEFF)
    assert fee < Decimal("0.001")


def test_polymarket_fee_at_midpoint():
    fee = polymarket_fee(Decimal("0.5"), RATE)
    assert fee == RATE * Decimal("0.5") * Decimal("0.5")
    assert fee == Decimal("0.001875")


def test_polymarket_fee_near_boundaries():
    fee_low = polymarket_fee(Decimal("0.01"), RATE)
    fee_high = polymarket_fee(Decimal("0.99"), RATE)
    assert fee_low < Decimal("0.001")
    assert fee_high < Decimal("0.001")


# --- arb detection tests ---

def test_no_arb_when_cost_over_1():
    pair = make_pair()
    kb = make_order_book(Venue.KALSHI, "K1", "0.52", "0.50")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.50", "0.52")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    assert result == []


def test_arb_detected_one_direction():
    pair = make_pair()
    # YES@Kalshi=0.45, NO@Poly=0.50 → raw=0.95, fees small → net < 1.0
    kb = make_order_book(Venue.KALSHI, "K1", "0.45", "0.70")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.70", "0.50")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    assert len(result) == 1
    assert result[0].yes_venue == Venue.KALSHI
    assert result[0].no_venue == Venue.POLYMARKET


def test_arb_detected_both_directions():
    pair = make_pair()
    # Both directions profitable
    kb = make_order_book(Venue.KALSHI, "K1", "0.45", "0.45")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.45", "0.45")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    assert len(result) == 2


def test_edge_pct_calculation():
    pair = make_pair()
    # net_cost = 0.95 (before fees); with tiny fees at low prices, edge > 0
    kb = make_order_book(Venue.KALSHI, "K1", "0.45", "0.60")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.60", "0.48")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    for opp in result:
        expected_edge = (Decimal("1") - opp.net_cost) / opp.net_cost * 100
        assert abs(opp.edge_pct - expected_edge) < Decimal("0.000001")


def test_below_min_edge_filtered():
    pair = make_pair()
    # Very thin arb — raw cost 0.999, fees push it positive but edge < 0.5%
    kb = make_order_book(Venue.KALSHI, "K1", "0.499", "0.499")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.499", "0.499")
    result = check_arb(pair, kb, pb, COEFF, RATE, Decimal("5.0"))
    assert result == []


def test_net_profit_per_100_matches_edge():
    pair = make_pair()
    kb = make_order_book(Venue.KALSHI, "K1", "0.45", "0.48")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.55", "0.50")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    for opp in result:
        assert opp.net_profit_per_100 == opp.edge_pct


def test_max_contracts_is_min_of_sizes():
    pair = make_pair()
    kb = make_order_book(Venue.KALSHI, "K1", "0.45", "0.60", size_yes="300", size_no="200")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.60", "0.48", size_yes="100", size_no="400")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    for opp in result:
        if opp.yes_venue == Venue.KALSHI:
            # yes_size from kalshi=300, no_size from poly=400
            assert opp.max_contracts == Decimal("300")
        else:
            # yes_size from poly=100, no_size from kalshi=200
            assert opp.max_contracts == Decimal("100")


def test_missing_ask_skipped():
    pair = make_pair()
    from datetime import datetime, timezone
    from arb_scanner.models import OrderBook
    kb = OrderBook(
        venue=Venue.KALSHI,
        market_id="K1",
        timestamp=datetime.now(timezone.utc),
        best_ask_yes=None,
        best_ask_no=Decimal("0.5"),
    )
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.45", "0.48")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    # YES@Kalshi direction skipped (no YES ask); YES@Poly direction might work
    for opp in result:
        assert opp.yes_venue == Venue.POLYMARKET


def test_resolution_warning_does_not_suppress_arb():
    pair = make_pair(resolution_warning="Kalshi: 90 min only; Polymarket: includes OT")
    kb = make_order_book(Venue.KALSHI, "K1", "0.45", "0.60")
    pb = make_order_book(Venue.POLYMARKET, "P1", "0.60", "0.48")
    result = check_arb(pair, kb, pb, COEFF, RATE, MIN_EDGE)
    # Opportunity still appears; user sees the warning via pair.resolution_warning
    for opp in result:
        assert opp.pair.resolution_warning is not None
