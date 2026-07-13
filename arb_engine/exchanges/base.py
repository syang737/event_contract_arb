"""Abstract exchange client interface shared by all venue integrations."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from ..config import MarketMapping
from ..core.models import Exchange, MarketBook

if TYPE_CHECKING:
    from ..mapping.models import VenueMarket


class ExchangeError(RuntimeError):
    """Base class for venue integration errors."""


class GeoBlockedError(ExchangeError):
    """Raised when a venue geo-blocks the request (treated as a hard error)."""


class RateLimitedError(ExchangeError):
    """Raised when a venue rate-limits the request."""


@dataclass(frozen=True)
class MarketResolution:
    """Outcome of a settled market, used by the settlement job."""

    resolved: bool
    yes_won: Optional[bool] = None  # True if YES settled to $1, False if NO, None if open

    @property
    def settlement_price_yes(self) -> Optional[float]:
        if not self.resolved or self.yes_won is None:
            return None
        return 1.0 if self.yes_won else 0.0


class ExchangeClient(abc.ABC):
    """Async market-data client for a single venue.

    Implementations must be usable as async context managers so transports
    (and their proxy connections) are cleaned up deterministically.
    """

    exchange: Exchange

    async def __aenter__(self) -> "ExchangeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @abc.abstractmethod
    async def fetch_book(self, mapping: MarketMapping) -> MarketBook:
        """Fetch and normalize the current order book for ``mapping``."""

    @abc.abstractmethod
    async def get_resolution(self, mapping: MarketMapping) -> MarketResolution:
        """Return whether the market has settled and which side won."""

    @abc.abstractmethod
    async def list_markets(self, **kwargs) -> "list[VenueMarket]":
        """Discover the venue's current markets as normalized ``VenueMarket``s.

        Used by the mapping pipeline to build/refresh the cross-venue mapping.
        """

    async def close(self) -> None:  # pragma: no cover - trivial default
        """Release any underlying transport resources."""
        return None
