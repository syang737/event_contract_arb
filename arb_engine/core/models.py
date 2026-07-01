"""Normalized, venue-agnostic domain models.

All prices are expressed in **dollars per contract** in ``[0.0, 1.0]`` and all
sizes in **contracts**, regardless of the venue's native representation
(Polymarket already quotes 0..1; Kalshi quotes integer cents, which the Kalshi
client divides by 100 before constructing these models).

Order-book sides are normalized so that ``asks`` always represent liquidity you
*consume when buying* that outcome, sorted by ascending price. That lets the
arbitrage engine treat both venues identically: buying YES or NO is always a
walk down the corresponding ``asks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utcnow() -> datetime:
    """Timezone-aware current UTC time (used everywhere for staleness math)."""
    return datetime.now(timezone.utc)


class Exchange(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class Direction(str, Enum):
    """Which venue supplies the YES leg and which supplies the NO leg."""

    YES_PM_NO_KA = "YES_PM_NO_KA"  # buy YES on Polymarket, buy NO on Kalshi
    YES_KA_NO_PM = "YES_KA_NO_PM"  # buy YES on Kalshi, buy NO on Polymarket


@dataclass(frozen=True)
class BookLevel:
    """A single price level. ``price`` in $/contract, ``size`` in contracts."""

    price: float
    size: float


@dataclass
class BookSide:
    """One outcome's book, normalized so ``asks`` are what you pay to buy.

    ``bids`` are sorted by descending price (best bid first); ``asks`` by
    ascending price (best ask first).
    """

    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def ask_depth(self) -> float:
        """Total contracts available across the ask side."""
        return sum(level.size for level in self.asks)

    @property
    def bid_depth(self) -> float:
        return sum(level.size for level in self.bids)

    def sorted(self) -> "BookSide":
        """Return a copy with bids/asks re-sorted into canonical order."""
        return BookSide(
            bids=sorted(self.bids, key=lambda lvl: lvl.price, reverse=True),
            asks=sorted(self.asks, key=lambda lvl: lvl.price),
        )


@dataclass
class MarketBook:
    """Both outcomes of a single market on a single venue at a point in time."""

    exchange: Exchange
    market_id: str  # internal mapping id (e.g. "us_pres_2028_dem")
    venue_market_id: str  # Polymarket conditionId or Kalshi ticker
    yes: BookSide
    no: BookSide
    ts: datetime = field(default_factory=utcnow)

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        now = now or utcnow()
        return (now - self.ts).total_seconds()

    def is_stale(self, max_age_seconds: float, now: Optional[datetime] = None) -> bool:
        return self.age_seconds(now) > max_age_seconds

    def side(self, side: Side) -> BookSide:
        return self.yes if side is Side.YES else self.no


@dataclass
class Quote:
    """Compact top-of-book snapshot for one outcome (persisted for sampling)."""

    exchange: Exchange
    market_id: str
    venue_market_id: str
    side: Side
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: float
    ask_size: float
    ts: datetime = field(default_factory=utcnow)

    @classmethod
    def from_book(cls, book: MarketBook, side: Side) -> "Quote":
        bs = book.side(side)
        return cls(
            exchange=book.exchange,
            market_id=book.market_id,
            venue_market_id=book.venue_market_id,
            side=side,
            best_bid=bs.best_bid,
            best_ask=bs.best_ask,
            bid_size=bs.bids[0].size if bs.bids else 0.0,
            ask_size=bs.asks[0].size if bs.asks else 0.0,
            ts=book.ts,
        )


@dataclass
class ArbLeg:
    """One side of a candidate arbitrage (a simulated buy)."""

    exchange: Exchange
    side: Side
    venue_market_id: str
    avg_price: float  # volume-weighted fill price, $/contract
    size: float  # contracts
    fee: float  # USD

    @property
    def cost(self) -> float:
        """Cash outlay for the fill, excluding fees."""
        return self.avg_price * self.size

    @property
    def cost_with_fee(self) -> float:
        return self.cost + self.fee


@dataclass
class ArbOpportunity:
    """A detected (but not necessarily executed) cross-exchange opportunity."""

    market_id: str
    label: str
    direction: Direction
    size: float
    leg_yes: ArbLeg
    leg_no: ArbLeg
    gross_edge_per_contract: float  # 1 - (avg_yes + avg_no)
    net_edge_per_contract: float  # gross - fees/size
    net_edge_per_contract_adj: float  # net - safety_buffer
    ts: datetime = field(default_factory=utcnow)

    @property
    def total_fee(self) -> float:
        return self.leg_yes.fee + self.leg_no.fee

    @property
    def total_cost(self) -> float:
        """Total cash outlay including fees (== hypothetical capital used)."""
        return self.leg_yes.cost_with_fee + self.leg_no.cost_with_fee

    @property
    def total_net_profit(self) -> float:
        """Expected profit at settlement after the safety buffer."""
        return self.net_edge_per_contract_adj * self.size

    def leg_for(self, exchange: Exchange) -> ArbLeg:
        return self.leg_yes if self.leg_yes.exchange is exchange else self.leg_no
