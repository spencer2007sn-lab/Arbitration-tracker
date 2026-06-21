from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx
import structlog

from arb_scanner.fetchers.base import BaseFetcher
from arb_scanner.models import NormalizedMarket, OrderBook, Venue

log = structlog.get_logger()

_ABBREV = [
    (r"\bvs\.?\b", "versus"),
    (r"\bfc\b", "football club"),
    (r"\butd\b", "united"),
    (r"\bman\b", "manchester"),
]

SPORT_TAG_MAP = {
    "soccer": "soccer",
    "football": "soccer",
    "basketball": "basketball",
    "nba": "basketball",
    "baseball": "baseball",
    "mlb": "baseball",
    "hockey": "hockey",
    "nhl": "hockey",
    "tennis": "tennis",
    "golf": "golf",
    "mma": "mma",
    "ufc": "mma",
    "boxing": "boxing",
    "nfl": "american football",
}


def _normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    for pattern, replacement in _ABBREV:
        t = re.sub(pattern, replacement, t)
    return re.sub(r"\s+", " ", t).strip()


def _detect_sport_from_tags(tags: list[dict]) -> Optional[str]:
    for tag in tags:
        label = tag.get("label", "").lower()
        if label in SPORT_TAG_MAP:
            return SPORT_TAG_MAP[label]
    return None


def _extract_teams(title: str) -> tuple[Optional[str], Optional[str]]:
    """Try to extract team names from 'Will Team A win?' or 'Team A vs Team B' style."""
    normalized = _normalize(title)
    m = re.search(
        r"^(?:will\s+)?([\w\s]+?)\s+(?:to win\s+)?versus\s+([\w\s]+?)(?:\s+(?:to win|win|wins?))?$",
        normalized,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # "Will X win?" pattern — only one team extractable
    m2 = re.search(r"will\s+([\w\s]+?)\s+win", normalized)
    if m2:
        return m2.group(1).strip(), None
    return None, None


class PolymarketFetcher(BaseFetcher):
    def __init__(
        self,
        gamma_url: str,
        clob_url: str,
        max_hours_to_close: int = 48,
        sport_filter: list[str] | None = None,
        poly_fee_rate: Decimal = Decimal("0.0075"),
    ) -> None:
        self._gamma_url = gamma_url.rstrip("/")
        self._clob_url = clob_url.rstrip("/")
        self._max_hours = max_hours_to_close
        self._sport_filter = [s.lower() for s in (sport_filter or [])]
        self._client = httpx.AsyncClient(timeout=15.0)

    async def fetch_markets(self) -> list[NormalizedMarket]:
        cutoff = datetime.now(timezone.utc) + timedelta(hours=self._max_hours)
        markets: list[NormalizedMarket] = []
        offset = 0
        limit = 100

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "tag": "sports",
                "limit": limit,
                "offset": offset,
            }
            r = await self._client.get(f"{self._gamma_url}/events", params=params)
            r.raise_for_status()
            events: list[dict] = r.json()

            if not events:
                break

            for event in events:
                end_str = event.get("endDate") or event.get("end_date_iso")
                if not end_str:
                    continue
                close_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if close_time > cutoff:
                    continue

                tags: list[dict] = event.get("tags", [])
                sport = _detect_sport_from_tags(tags)
                if self._sport_filter and (not sport or sport not in self._sport_filter):
                    continue

                for mkt in event.get("markets", []):
                    if not mkt.get("active") or mkt.get("closed"):
                        continue
                    clob_ids: list[str] = mkt.get("clobTokenIds", [])
                    if len(clob_ids) < 2:
                        continue

                    question = mkt.get("question", event.get("title", ""))
                    team_a, team_b = _extract_teams(question)

                    markets.append(NormalizedMarket(
                        venue=Venue.POLYMARKET,
                        market_id=mkt["conditionId"],
                        title=question,
                        normalized_title=_normalize(question),
                        sport=sport,
                        team_a=team_a,
                        team_b=team_b,
                        outcome_type="moneyline",
                        close_time=close_time,
                        token_id_yes=clob_ids[0],
                        token_id_no=clob_ids[1],
                    ))

            offset += limit
            if len(events) < limit:
                break

        log.info("polymarket_markets_fetched", count=len(markets))
        return markets

    async def fetch_order_book(self, market: NormalizedMarket) -> OrderBook:
        if not market.token_id_yes or not market.token_id_no:
            return OrderBook(
                venue=Venue.POLYMARKET,
                market_id=market.market_id,
                timestamp=datetime.now(timezone.utc),
            )

        yes_ask, yes_size = await self._best_ask(market.token_id_yes)
        no_ask, no_size = await self._best_ask(market.token_id_no)

        return OrderBook(
            venue=Venue.POLYMARKET,
            market_id=market.market_id,
            timestamp=datetime.now(timezone.utc),
            best_ask_yes=yes_ask,
            best_ask_no=no_ask,
            size_at_ask_yes=yes_size,
            size_at_ask_no=no_size,
        )

    async def _best_ask(self, token_id: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
        r = await self._client.get(
            f"{self._clob_url}/book",
            params={"token_id": token_id},
        )
        r.raise_for_status()
        data = r.json()
        asks: list[dict] = data.get("asks", [])
        if not asks:
            return None, None
        # asks are sorted ascending — first entry is the best ask
        best = asks[0]
        return Decimal(str(best["price"])), Decimal(str(best["size"]))

    async def close(self) -> None:
        await self._client.aclose()
