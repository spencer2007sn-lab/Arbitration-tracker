from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from arb_scanner.fetchers.base import BaseFetcher
from arb_scanner.models import NormalizedMarket, OrderBook, Venue

log = structlog.get_logger()

_ABBREV = [
    (r"\bvs\.?\b", "versus"),
    (r"\bfc\b", "football club"),
    (r"\butd\b", "united"),
    (r"\bman\b", "manchester"),
]

SPORT_KEYWORDS = {
    "soccer": ["soccer", "football", "epl", "mls", "la liga", "bundesliga", "serie a"],
    "basketball": ["basketball", "nba", "ncaa"],
    "baseball": ["baseball", "mlb"],
    "hockey": ["hockey", "nhl"],
    "tennis": ["tennis", "atp", "wta"],
    "golf": ["golf", "pga"],
    "mma": ["mma", "ufc"],
    "boxing": ["boxing"],
    "american football": ["nfl", "american football"],
}


def _normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    for pattern, replacement in _ABBREV:
        t = re.sub(pattern, replacement, t)
    return re.sub(r"\s+", " ", t).strip()


def _detect_sport(tags: list[str], title: str) -> Optional[str]:
    combined = " ".join(tags + [title]).lower()
    for sport, keywords in SPORT_KEYWORDS.items():
        if any(k in combined for k in keywords):
            return sport
    return None


def _extract_teams(title: str) -> tuple[Optional[str], Optional[str]]:
    """Try to extract team A and B from a 'Team A to win vs Team B' style title."""
    normalized = _normalize(title)
    # Pattern: "X to win versus Y" or "X win versus Y" or "X versus Y"
    m = re.search(
        r"^([\w\s]+?)\s+(?:to win\s+)?versus\s+([\w\s]+?)(?:\s+(?:to win|wins?))?$",
        normalized,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _sign_request(private_key, method: str, path: str) -> tuple[str, str]:
    """Return (timestamp_ms_str, base64_signature)."""
    ts = str(int(time.time() * 1000))
    message = (ts + method.upper() + path).encode()
    sig = private_key.sign(message, padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.MAX_LENGTH,
    ), hashes.SHA256())
    return ts, base64.b64encode(sig).decode()


class KalshiFetcher(BaseFetcher):
    def __init__(
        self,
        api_key: str,
        private_key_path: Path,
        base_url: str,
        max_hours_to_close: int = 48,
        sport_filter: list[str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_hours = max_hours_to_close
        self._sport_filter = [s.lower() for s in (sport_filter or [])]
        self._client = httpx.AsyncClient(timeout=15.0)

        self._private_key = None
        if private_key_path.exists() and api_key:
            pem = private_key_path.read_bytes()
            self._private_key = serialization.load_pem_private_key(pem, password=None)

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key or not self._api_key:
            return {}
        ts, sig = _sign_request(self._private_key, method, path)
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url_path = f"/trade-api/v2{path}"
        headers = self._auth_headers("GET", url_path)
        r = await self._client.get(
            self._base_url + path,
            params=params,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()

    async def fetch_markets(self) -> list[NormalizedMarket]:
        cutoff = datetime.now(timezone.utc) + timedelta(hours=self._max_hours)
        markets: list[NormalizedMarket] = []
        cursor: Optional[str] = None

        while True:
            params: dict = {"status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor

            data = await self._get("/markets", params)
            raw_markets = data.get("markets", [])

            for m in raw_markets:
                close_str = m.get("close_time") or m.get("expiration_time")
                if not close_str:
                    continue
                close_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if close_time > cutoff:
                    continue

                category = (m.get("category") or "").lower()
                tags = [t.lower() for t in (m.get("tags") or [])]
                title = m.get("title", "")

                sport = _detect_sport(tags + [category], title)
                if self._sport_filter and (not sport or sport not in self._sport_filter):
                    continue

                team_a, team_b = _extract_teams(title)
                markets.append(NormalizedMarket(
                    venue=Venue.KALSHI,
                    market_id=m["ticker"],
                    title=title,
                    normalized_title=_normalize(title),
                    sport=sport,
                    team_a=team_a,
                    team_b=team_b,
                    outcome_type="moneyline",
                    close_time=close_time,
                    resolution_notes=m.get("yes_sub_title"),
                ))

            cursor = data.get("cursor")
            if not cursor or not raw_markets:
                break

        log.info("kalshi_markets_fetched", count=len(markets))
        return markets

    async def fetch_order_book(self, market: NormalizedMarket) -> OrderBook:
        path = f"/markets/{market.market_id}/orderbook"
        data = await self._get(path)

        # Kalshi returns bids only. Prices are in cents (integer).
        ob = data.get("orderbook", {})
        yes_bids = ob.get("yes", [])  # [[price_cents, size], ...]
        no_bids = ob.get("no", [])

        best_ask_yes, size_at_ask_yes = _invert_bids(no_bids)
        best_ask_no, size_at_ask_no = _invert_bids(yes_bids)

        return OrderBook(
            venue=Venue.KALSHI,
            market_id=market.market_id,
            timestamp=datetime.now(timezone.utc),
            best_ask_yes=best_ask_yes,
            best_ask_no=best_ask_no,
            size_at_ask_yes=size_at_ask_yes,
            size_at_ask_no=size_at_ask_no,
        )

    async def close(self) -> None:
        await self._client.aclose()


def _invert_bids(
    bids: list,
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Given a list of [price_cents, size] bids, return (best_ask, size_at_ask).

    The best ask on one side equals 1 - (best bid on the other side / 100).
    The bids list may contain either [int, int] or [str, str] depending on
    which Kalshi endpoint is used (orderbook vs orderbook_fp).
    """
    if not bids:
        return None, None

    best_price_cents: Optional[Decimal] = None
    best_size: Optional[Decimal] = None

    for row in bids:
        price_cents = Decimal(str(row[0]))
        size = Decimal(str(row[1]))
        if best_price_cents is None or price_cents > best_price_cents:
            best_price_cents = price_cents
            best_size = size

    if best_price_cents is None:
        return None, None

    best_ask = (Decimal("100") - best_price_cents) / Decimal("100")
    return best_ask, best_size
