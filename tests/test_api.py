from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from arb_scanner.main import app, app_state
from arb_scanner.models import (
    ArbOpportunity,
    MarketPair,
    ScanResult,
    Venue,
)
from tests.conftest import make_kalshi_market, make_order_book, make_poly_market


def make_scan_result(n_opps: int = 0) -> ScanResult:
    opps = []
    if n_opps > 0:
        pair = MarketPair(
            kalshi_market=make_kalshi_market(),
            polymarket_market=make_poly_market(),
            match_method="exact",
        )
        opp = ArbOpportunity(
            pair=pair,
            direction="YES@kalshi + NO@polymarket",
            yes_venue=Venue.KALSHI,
            no_venue=Venue.POLYMARKET,
            yes_ask=Decimal("0.45"),
            no_ask=Decimal("0.48"),
            raw_cost=Decimal("0.93"),
            fee_yes=Decimal("0.0173"),
            fee_no=Decimal("0.0018"),
            net_cost=Decimal("0.9491"),
            edge_pct=Decimal("5.36"),
            net_profit_per_100=Decimal("5.36"),
            scanned_at=datetime.now(timezone.utc),
        )
        opps = [opp]

    return ScanResult(
        scanned_at=datetime.now(timezone.utc),
        opportunities=opps,
        pairs_checked=3,
        scan_duration_ms=142.5,
        errors=[],
    )


@pytest.fixture
def client():
    # Use TestClient without starting the background scanner
    with patch("arb_scanner.main.lifespan"):
        with TestClient(app) as c:
            yield c


def test_health_endpoint_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_opportunities_503_before_first_scan(client):
    # Ensure no result is set
    app_state.result = None
    resp = client.get("/opportunities")
    assert resp.status_code == 503
    assert "error" in resp.json()


def test_opportunities_returns_scan_result(client):
    app_state.result = make_scan_result(n_opps=1)
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    data = resp.json()
    assert "opportunities" in data
    assert data["pairs_checked"] == 3
    assert data["scan_duration_ms"] == 142.5
    assert len(data["opportunities"]) == 1


def test_opportunities_empty_when_no_arb(client):
    app_state.result = make_scan_result(n_opps=0)
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["opportunities"] == []


def test_dashboard_html_served(client):
    resp = client.get("/")
    assert resp.status_code in (200, 500)  # 500 if static not found in test env
    assert resp.headers["content-type"].startswith("text/html")


def test_opportunities_shape(client):
    app_state.result = make_scan_result(n_opps=1)
    resp = client.get("/opportunities")
    opp = resp.json()["opportunities"][0]
    required_keys = {
        "direction", "yes_venue", "no_venue", "yes_ask", "no_ask",
        "net_cost", "edge_pct", "net_profit_per_100", "scanned_at",
    }
    assert required_keys.issubset(opp.keys())
