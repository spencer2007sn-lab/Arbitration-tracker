from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, model_validator


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class OrderBook(BaseModel):
    venue: Venue
    market_id: str
    timestamp: datetime
    best_ask_yes: Optional[Decimal] = None
    best_ask_no: Optional[Decimal] = None
    size_at_ask_yes: Optional[Decimal] = None
    size_at_ask_no: Optional[Decimal] = None


class NormalizedMarket(BaseModel):
    venue: Venue
    market_id: str
    title: str
    normalized_title: str
    sport: Optional[str] = None
    team_a: Optional[str] = None
    team_b: Optional[str] = None
    outcome_type: str = "moneyline"  # "moneyline" | "over_under" | "spread" | "other"
    threshold: Optional[Decimal] = None
    close_time: datetime
    resolution_notes: Optional[str] = None
    token_id_yes: Optional[str] = None  # Polymarket only
    token_id_no: Optional[str] = None   # Polymarket only


class MarketPair(BaseModel):
    kalshi_market: NormalizedMarket
    polymarket_market: NormalizedMarket
    match_method: str  # "manual" | "exact" | "fuzzy"
    match_score: Optional[float] = None
    resolution_warning: Optional[str] = None


class ArbOpportunity(BaseModel):
    pair: MarketPair
    direction: str
    yes_venue: Venue
    no_venue: Venue
    yes_ask: Decimal
    no_ask: Decimal
    yes_size: Optional[Decimal] = None
    no_size: Optional[Decimal] = None
    raw_cost: Decimal
    fee_yes: Decimal
    fee_no: Decimal
    net_cost: Decimal
    edge_pct: Decimal
    net_profit_per_100: Decimal
    max_contracts: Optional[Decimal] = None
    max_profit: Optional[Decimal] = None
    scanned_at: datetime

    @model_validator(mode="after")
    def verify_is_arb(self) -> "ArbOpportunity":
        if self.net_cost >= Decimal("1.0"):
            raise ValueError(f"Not an arb: net_cost={self.net_cost}")
        return self


class ScanResult(BaseModel):
    scanned_at: datetime
    opportunities: list[ArbOpportunity]
    pairs_checked: int
    kalshi_markets: int = 0
    polymarket_markets: int = 0
    scan_duration_ms: float
    errors: list[str]
