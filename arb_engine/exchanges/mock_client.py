"""In-memory mock client so the engine runs fully offline (demos + tests).

``MockClient`` returns pre-seeded :class:`MarketBook` snapshots (with a fresh
timestamp on every fetch, so staleness filters pass) and configurable
resolutions. :func:`build_demo_clients` wires up a Polymarket/Kalshi pair whose
books contain one obvious cross-exchange arb and one non-arb market, which is
what ``run-bot --mock`` exercises.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..config import AppConfig, MarketMapping
from ..core.models import BookLevel, BookSide, Exchange, MarketBook, utcnow
from .base import ExchangeClient, ExchangeError, MarketResolution


class MockClient(ExchangeClient):
    """Serves canned books/resolutions for a single venue."""

    def __init__(
        self,
        exchange: Exchange,
        books: Optional[dict[str, MarketBook]] = None,
        resolutions: Optional[dict[str, MarketResolution]] = None,
    ):
        self.exchange = exchange
        self._books: dict[str, MarketBook] = dict(books or {})
        self._resolutions: dict[str, MarketResolution] = dict(resolutions or {})

    def set_book(self, market_id: str, book: MarketBook) -> None:
        self._books[market_id] = book

    def set_resolution(self, market_id: str, resolution: MarketResolution) -> None:
        self._resolutions[market_id] = resolution

    async def fetch_book(self, mapping: MarketMapping) -> MarketBook:
        book = self._books.get(mapping.id)
        if book is None:
            raise ExchangeError(
                f"MockClient[{self.exchange.value}] has no book for {mapping.id!r}"
            )
        # Return with a fresh timestamp so the staleness filter never trips.
        return replace(book, ts=utcnow())

    async def get_resolution(self, mapping: MarketMapping) -> MarketResolution:
        return self._resolutions.get(mapping.id, MarketResolution(resolved=False))


def _side(levels: list[tuple[float, float]], *, asks: bool) -> BookSide:
    book_levels = [BookLevel(price=p, size=s) for p, s in levels]
    if asks:
        return BookSide(asks=book_levels).sorted()
    return BookSide(bids=book_levels).sorted()


def _book(exchange: Exchange, mapping: MarketMapping, yes_asks, no_asks) -> MarketBook:
    return MarketBook(
        exchange=exchange,
        market_id=mapping.id,
        venue_market_id=(
            mapping.polymarket.market_id
            if exchange is Exchange.POLYMARKET
            else mapping.kalshi.ticker
        ),
        yes=_side(yes_asks, asks=True),
        no=_side(no_asks, asks=True),
        ts=utcnow(),
    )


def build_demo_clients(config: AppConfig) -> tuple[MockClient, MockClient]:
    """Build a (polymarket, kalshi) mock pair with a visible arb on market #1.

    Market #1 (D1): YES cheap on Polymarket + NO cheap on Kalshi sum to ~0.92,
    a clean ~8c/contract gross edge. Market #2 (if present) is priced with no
    arb so the filtering path is exercised too.
    """
    pm = MockClient(Exchange.POLYMARKET)
    ka = MockClient(Exchange.KALSHI)

    for idx, mapping in enumerate(config.markets):
        if idx == 0:
            # Arb: buy YES on PM (~0.44) + NO on KA (~0.48) -> ~0.92 total.
            pm.set_book(
                mapping.id,
                _book(
                    Exchange.POLYMARKET,
                    mapping,
                    yes_asks=[(0.44, 200), (0.45, 300), (0.47, 500)],
                    no_asks=[(0.55, 400), (0.57, 400)],
                ),
            )
            ka.set_book(
                mapping.id,
                _book(
                    Exchange.KALSHI,
                    mapping,
                    yes_asks=[(0.53, 300), (0.55, 300)],
                    no_asks=[(0.48, 250), (0.49, 350), (0.51, 400)],
                ),
            )
        else:
            # No arb: both directions sum above 1.
            pm.set_book(
                mapping.id,
                _book(
                    Exchange.POLYMARKET,
                    mapping,
                    yes_asks=[(0.52, 300)],
                    no_asks=[(0.51, 300)],
                ),
            )
            ka.set_book(
                mapping.id,
                _book(
                    Exchange.KALSHI,
                    mapping,
                    yes_asks=[(0.53, 300)],
                    no_asks=[(0.52, 300)],
                ),
            )

    return pm, ka
