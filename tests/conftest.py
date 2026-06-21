from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb_scanner.models import NormalizedMarket, OrderBook, Venue

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def kalshi_market_fixture() -> dict:
    return load_fixture("kalshi_orderbook.json")


@pytest.fixture
def polymarket_events_fixture() -> list:
    return load_fixture("polymarket_events.json")


@pytest.fixture
def poly_book_yes_fixture() -> dict:
    return load_fixture("polymarket_book_yes.json")


@pytest.fixture
def poly_book_no_fixture() -> dict:
    return load_fixture("polymarket_book_no.json")


def make_kalshi_market(
    ticker: str = "KXTEST-25JUN21-AAA",
    title: str = "Team A to win vs Team B",
    close_hours: int = 4,
    sport: str = "soccer",
    team_a: str = "team a",
    team_b: str = "team b",
) -> NormalizedMarket:
    return NormalizedMarket(
        venue=Venue.KALSHI,
        market_id=ticker,
        title=title,
        normalized_title=title.lower(),
        sport=sport,
        team_a=team_a,
        team_b=team_b,
        outcome_type="moneyline",
        close_time=datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),
    )


def make_poly_market(
    condition_id: str = "0xabc",
    title: str = "Will Team A win?",
    close_hours: int = 4,
    sport: str = "soccer",
    team_a: str = "team a",
    team_b: str = "team b",
) -> NormalizedMarket:
    return NormalizedMarket(
        venue=Venue.POLYMARKET,
        market_id=condition_id,
        title=title,
        normalized_title=title.lower(),
        sport=sport,
        team_a=team_a,
        team_b=team_b,
        outcome_type="moneyline",
        close_time=datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),
        token_id_yes="111111111",
        token_id_no="222222222",
    )


def make_order_book(
    venue: Venue,
    market_id: str,
    best_ask_yes: str,
    best_ask_no: str,
    size_yes: str = "200",
    size_no: str = "150",
) -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id=market_id,
        timestamp=datetime.now(timezone.utc),
        best_ask_yes=Decimal(best_ask_yes),
        best_ask_no=Decimal(best_ask_no),
        size_at_ask_yes=Decimal(size_yes),
        size_at_ask_no=Decimal(size_no),
    )
