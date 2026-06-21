from __future__ import annotations

from abc import ABC, abstractmethod

from arb_scanner.models import NormalizedMarket, OrderBook


class BaseFetcher(ABC):
    @abstractmethod
    async def fetch_markets(self) -> list[NormalizedMarket]:
        """Return all active, relevant markets from this venue."""
        ...

    @abstractmethod
    async def fetch_order_book(self, market: NormalizedMarket) -> OrderBook:
        """Return the current best-ask prices for YES and NO on a single market."""
        ...

    async def close(self) -> None:
        """Tear down any open HTTP clients or connections."""
        ...
