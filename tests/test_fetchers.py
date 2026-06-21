from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from arb_scanner.fetchers.kalshi import KalshiFetcher, _invert_bids
from arb_scanner.fetchers.polymarket import PolymarketFetcher
from arb_scanner.models import Venue

FIXTURES = Path(__file__).parent / "fixtures"

KALSHI_BASE = "https://api.kalshi.com/trade-api/v2"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


def make_kalshi(private_key_path: Path = Path("/nonexistent")) -> KalshiFetcher:
    return KalshiFetcher(
        api_key="test-key",
        private_key_path=private_key_path,
        base_url=KALSHI_BASE,
        max_hours_to_close=72,  # wide window for fixtures
    )


def make_poly() -> PolymarketFetcher:
    return PolymarketFetcher(
        gamma_url=GAMMA_URL,
        clob_url=CLOB_URL,
        max_hours_to_close=72,
    )


# --- _invert_bids unit tests (no network) ---

def test_invert_bids_normal():
    bids = [[10, 200], [8, 400]]  # best bid = 10¢ → best ask = 90¢
    ask, size = _invert_bids(bids)
    assert ask == Decimal("0.90")
    assert size == Decimal("200")


def test_invert_bids_picks_highest():
    bids = [[8, 100], [12, 150], [5, 300]]
    ask, size = _invert_bids(bids)
    assert ask == Decimal("0.88")  # 100 - 12
    assert size == Decimal("150")


def test_invert_bids_empty():
    ask, size = _invert_bids([])
    assert ask is None
    assert size is None


def test_invert_bids_string_prices():
    # orderbook_fp format uses strings
    bids = [["88", "500"], ["85", "1000"]]
    ask, size = _invert_bids(bids)
    assert ask == Decimal("0.12")  # 100 - 88


# --- Kalshi order book fetch ---

@pytest.mark.asyncio
async def test_kalshi_orderbook_inversion(httpx_mock: HTTPXMock):
    ob_data = json.loads((FIXTURES / "kalshi_orderbook.json").read_text())
    httpx_mock.add_response(
        url=f"{KALSHI_BASE}/markets/KXTEST-25JUN21-AAA/orderbook",
        json=ob_data,
    )

    fetcher = make_kalshi()
    from tests.conftest import make_kalshi_market
    market = make_kalshi_market()
    book = await fetcher.fetch_order_book(market)

    # Fixture: yes bids = [[88,500],[85,1000]], no bids = [[10,200],[8,400]]
    # best YES ask = 1 - 10/100 = 0.90
    # best NO ask  = 1 - 88/100 = 0.12
    assert book.best_ask_yes == Decimal("0.90")
    assert book.best_ask_no == Decimal("0.12")
    assert book.venue == Venue.KALSHI
    await fetcher.close()


@pytest.mark.asyncio
async def test_kalshi_orderbook_empty_side(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{KALSHI_BASE}/markets/KXTEST-25JUN21-AAA/orderbook",
        json={"orderbook": {"yes": [[88, 500]], "no": []}},
    )
    fetcher = make_kalshi()
    from tests.conftest import make_kalshi_market
    market = make_kalshi_market()
    book = await fetcher.fetch_order_book(market)

    assert book.best_ask_yes is None   # no NO bids → can't infer YES ask
    assert book.best_ask_no == Decimal("0.12")  # 1 - 88/100
    await fetcher.close()


@pytest.mark.asyncio
async def test_kalshi_market_list_filters_by_close_time(httpx_mock: HTTPXMock):
    now = datetime.now(timezone.utc)
    future_close = (now + timedelta(hours=100)).isoformat().replace("+00:00", "Z")
    near_close = (now + timedelta(hours=4)).isoformat().replace("+00:00", "Z")

    markets_data = {
        "markets": [
            {
                "ticker": "CLOSE-SOON",
                "title": "Team A to win",
                "category": "sports",
                "status": "open",
                "close_time": near_close,
                "tags": ["soccer"],
            },
            {
                "ticker": "CLOSE-FAR",
                "title": "Team B to win",
                "category": "sports",
                "status": "open",
                "close_time": future_close,
                "tags": ["soccer"],
            },
        ],
        "cursor": None,
    }
    httpx_mock.add_response(url=re.compile(r".*/markets.*"), json=markets_data)

    fetcher = make_kalshi()
    markets = await fetcher.fetch_markets()
    tickers = [m.market_id for m in markets]
    assert "CLOSE-SOON" in tickers
    assert "CLOSE-FAR" not in tickers
    await fetcher.close()


# --- Polymarket order book fetch ---

@pytest.mark.asyncio
async def test_polymarket_best_ask_from_first_element(httpx_mock: HTTPXMock):
    yes_data = json.loads((FIXTURES / "polymarket_book_yes.json").read_text())
    no_data = json.loads((FIXTURES / "polymarket_book_no.json").read_text())

    httpx_mock.add_response(
        url=f"{CLOB_URL}/book?token_id=111111111",
        json=yes_data,
    )
    httpx_mock.add_response(
        url=f"{CLOB_URL}/book?token_id=222222222",
        json=no_data,
    )

    fetcher = make_poly()
    from tests.conftest import make_poly_market
    market = make_poly_market()
    book = await fetcher.fetch_order_book(market)

    assert book.best_ask_yes == Decimal("0.89")
    assert book.best_ask_no == Decimal("0.09")
    assert book.venue == Venue.POLYMARKET
    await fetcher.close()


@pytest.mark.asyncio
async def test_polymarket_empty_book(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{CLOB_URL}/book?token_id=111111111",
        json={"market": "0xabc", "asset_id": "111", "bids": [], "asks": []},
    )
    httpx_mock.add_response(
        url=f"{CLOB_URL}/book?token_id=222222222",
        json={"market": "0xabc", "asset_id": "222", "bids": [], "asks": []},
    )

    fetcher = make_poly()
    from tests.conftest import make_poly_market
    market = make_poly_market()
    book = await fetcher.fetch_order_book(market)

    assert book.best_ask_yes is None
    assert book.best_ask_no is None
    await fetcher.close()


@pytest.mark.asyncio
async def test_polymarket_market_filter_by_close_time(httpx_mock: HTTPXMock):
    now = datetime.now(timezone.utc)
    near = (now + timedelta(hours=4)).isoformat().replace("+00:00", "Z")
    far = (now + timedelta(hours=100)).isoformat().replace("+00:00", "Z")

    events = [
        {
            "id": "e1",
            "title": "Event Near",
            "endDate": near,
            "active": True,
            "closed": False,
            "tags": [{"label": "Soccer"}],
            "markets": [{"id": "m1", "question": "Will A win?", "conditionId": "0x1",
                          "clobTokenIds": ["t1", "t2"], "active": True, "closed": False}],
        },
        {
            "id": "e2",
            "title": "Event Far",
            "endDate": far,
            "active": True,
            "closed": False,
            "tags": [{"label": "Soccer"}],
            "markets": [{"id": "m2", "question": "Will B win?", "conditionId": "0x2",
                          "clobTokenIds": ["t3", "t4"], "active": True, "closed": False}],
        },
    ]
    httpx_mock.add_response(url=re.compile(r".*/events.*"), json=events)

    fetcher = make_poly()
    markets = await fetcher.fetch_markets()
    ids = [m.market_id for m in markets]
    assert "0x1" in ids
    assert "0x2" not in ids
    await fetcher.close()
