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
from ..mapping.models import VenueMarket
from .base import ExchangeClient, ExchangeError, MarketResolution


class MockClient(ExchangeClient):
    """Serves canned books/resolutions for a single venue."""

    def __init__(
        self,
        exchange: Exchange,
        books: Optional[dict[str, MarketBook]] = None,
        resolutions: Optional[dict[str, MarketResolution]] = None,
        catalog: Optional[list[VenueMarket]] = None,
    ):
        self.exchange = exchange
        self._books: dict[str, MarketBook] = dict(books or {})
        self._resolutions: dict[str, MarketResolution] = dict(resolutions or {})
        self._catalog: list[VenueMarket] = list(catalog or [])

    def set_book(self, market_id: str, book: MarketBook) -> None:
        self._books[market_id] = book

    def set_resolution(self, market_id: str, resolution: MarketResolution) -> None:
        self._resolutions[market_id] = resolution

    def set_catalog(self, catalog: list[VenueMarket]) -> None:
        self._catalog = list(catalog)

    async def list_markets(self, **kwargs) -> list[VenueMarket]:
        return list(self._catalog)

    async def fetch_book(self, mapping: MarketMapping) -> MarketBook:
        book = self._books.get(mapping.id)
        if book is None:
            # Fall back to matching by venue id, so dynamically-loaded mappings
            # (whose internal id differs) still resolve to the seeded book.
            venue_id = (
                mapping.polymarket.market_id
                if self.exchange is Exchange.POLYMARKET
                else mapping.kalshi.ticker
            )
            book = next(
                (b for b in self._books.values() if b.venue_market_id == venue_id), None
            )
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


def build_demo_catalog(config: AppConfig) -> tuple[list[VenueMarket], list[VenueMarket]]:
    """Synthesize (polymarket, kalshi) catalogs the matcher can pair up.

    Each configured mapping yields an equivalent PM+KA market (ids kept aligned
    with the mapping so downstream books line up), plus a decoy on each side that
    must *not* match, to exercise blocking/scoring.
    """
    pm_catalog: list[VenueMarket] = []
    ka_catalog: list[VenueMarket] = []
    for mapping in config.markets:
        subject = mapping.label.split(":")[0].strip() or mapping.label
        pm_catalog.append(
            VenueMarket(
                exchange=Exchange.POLYMARKET,
                venue_id=mapping.polymarket.market_id,
                title=f"Will {subject.lower()} happen? {mapping.label}",
                description=f"Resolves YES if: {mapping.label}.",
                category=mapping.category,
                close_time=mapping.close_time,
                status="active",
                yes_token=mapping.polymarket.yes_token,
                no_token=mapping.polymarket.no_token,
                strike_type="binary",
                event_key=mapping.id,
            )
        )
        ka_catalog.append(
            VenueMarket(
                exchange=Exchange.KALSHI,
                venue_id=mapping.kalshi.ticker,
                title=f"{subject} — {mapping.label}",
                description=f"Market settles YES when {mapping.label}.",
                category=mapping.category,
                close_time=mapping.close_time,
                status="active",
                event_key=mapping.kalshi.ticker,
            )
        )
    # Decoys: same category, unrelated subject / far-off date -> should not match.
    pm_catalog.append(
        VenueMarket(
            exchange=Exchange.POLYMARKET,
            venue_id="0xdecoy_pm",
            title="Will it rain in Seattle on New Year's Day?",
            category="weather",
            status="active",
            yes_token="PM_DECOY_YES",
            no_token="PM_DECOY_NO",
            strike_type="binary",
        )
    )
    ka_catalog.append(
        VenueMarket(
            exchange=Exchange.KALSHI,
            venue_id="DECOY-KA",
            title="High temperature in Miami above 90F tomorrow",
            category="weather",
            status="active",
            strike_type="greater",
            strike=90.0,
        )
    )
    return pm_catalog, ka_catalog


def build_demo_clients(config: AppConfig) -> tuple[MockClient, MockClient]:
    """Build a (polymarket, kalshi) mock pair with a visible arb on market #1.

    Market #1 (D1): YES cheap on Polymarket + NO cheap on Kalshi sum to ~0.92,
    a clean ~8c/contract gross edge. Market #2 (if present) is priced with no
    arb so the filtering path is exercised too. Both clients also carry a
    synthetic discovery catalog (see :func:`build_demo_catalog`).
    """
    pm = MockClient(Exchange.POLYMARKET)
    ka = MockClient(Exchange.KALSHI)
    pm_catalog, ka_catalog = build_demo_catalog(config)
    pm.set_catalog(pm_catalog)
    ka.set_catalog(ka_catalog)

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
