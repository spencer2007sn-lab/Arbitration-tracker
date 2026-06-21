from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from arb_scanner.models import ArbOpportunity, MarketPair, OrderBook, Venue


def kalshi_fee(price: Decimal, coeff: Decimal = Decimal("0.07")) -> Decimal:
    """Taker fee per contract: coeff × price × (1 − price)."""
    return coeff * price * (Decimal("1") - price)


def polymarket_fee(price: Decimal, rate: Decimal = Decimal("0.0075")) -> Decimal:
    """Taker fee per contract: rate × price × (1 − price)."""
    return rate * price * (Decimal("1") - price)


def fee_for_venue(
    venue: Venue,
    price: Decimal,
    kalshi_coeff: Decimal,
    poly_rate: Decimal,
) -> Decimal:
    if venue == Venue.KALSHI:
        return kalshi_fee(price, kalshi_coeff)
    return polymarket_fee(price, poly_rate)


def check_arb(
    pair: MarketPair,
    kalshi_book: OrderBook,
    poly_book: OrderBook,
    kalshi_coeff: Decimal,
    poly_rate: Decimal,
    min_edge_pct: Decimal,
) -> list[ArbOpportunity]:
    """Check both trade directions for a matched market pair.

    Returns a list of ArbOpportunity (0, 1, or 2 items).
    """
    directions = [
        (
            Venue.KALSHI,
            Venue.POLYMARKET,
            kalshi_book.best_ask_yes,
            poly_book.best_ask_no,
            kalshi_book.size_at_ask_yes,
            poly_book.size_at_ask_no,
        ),
        (
            Venue.POLYMARKET,
            Venue.KALSHI,
            poly_book.best_ask_yes,
            kalshi_book.best_ask_no,
            poly_book.size_at_ask_yes,
            kalshi_book.size_at_ask_no,
        ),
    ]

    opportunities: list[ArbOpportunity] = []
    now = datetime.now(timezone.utc)

    for yes_venue, no_venue, yes_ask, no_ask, yes_size, no_size in directions:
        if yes_ask is None or no_ask is None:
            continue

        raw_cost = yes_ask + no_ask
        fee_yes = fee_for_venue(yes_venue, yes_ask, kalshi_coeff, poly_rate)
        fee_no = fee_for_venue(no_venue, no_ask, kalshi_coeff, poly_rate)
        net_cost = raw_cost + fee_yes + fee_no

        if net_cost >= Decimal("1"):
            continue

        edge_pct = (Decimal("1") - net_cost) / net_cost * Decimal("100")
        if edge_pct < min_edge_pct:
            continue

        max_contracts: Optional[Decimal] = None
        max_profit: Optional[Decimal] = None
        if yes_size is not None and no_size is not None:
            max_contracts = min(yes_size, no_size)
            max_profit = max_contracts * (Decimal("1") - net_cost)

        try:
            opp = ArbOpportunity(
                pair=pair,
                direction=f"YES@{yes_venue.value} + NO@{no_venue.value}",
                yes_venue=yes_venue,
                no_venue=no_venue,
                yes_ask=yes_ask,
                no_ask=no_ask,
                yes_size=yes_size,
                no_size=no_size,
                raw_cost=raw_cost,
                fee_yes=fee_yes,
                fee_no=fee_no,
                net_cost=net_cost,
                edge_pct=edge_pct,
                net_profit_per_100=edge_pct,
                max_contracts=max_contracts,
                max_profit=max_profit,
                scanned_at=now,
            )
            opportunities.append(opp)
        except ValueError:
            pass  # model_validator rejected it — shouldn't happen but be defensive

    return opportunities
