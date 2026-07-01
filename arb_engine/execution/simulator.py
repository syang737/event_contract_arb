"""Trade simulation: turn a detected opportunity into a paper trade.

Re-walks the latest books for both legs (spec 8.1 step 1) so the persisted
trade reflects an executable fill: if the book thinned between detection and
"execution", the trade is downsized to what both legs can actually fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..config import MarketMapping
from ..core.fees import FeeEngine
from ..core.models import (
    ArbOpportunity,
    Direction,
    Exchange,
    MarketBook,
    Side,
    utcnow,
)
from ..core.orderbook import walk_book

_EPS = 1e-9


@dataclass
class SimFill:
    exchange: Exchange
    side: Side
    venue_market_id: str
    price: float
    size: float


@dataclass
class SimLeg:
    exchange: Exchange
    side: Side
    venue_market_id: str
    avg_price: float
    size: float
    fee: float

    @property
    def cost(self) -> float:
        return self.avg_price * self.size

    @property
    def cost_with_fee(self) -> float:
        return self.cost + self.fee


@dataclass
class SimulatedTrade:
    market_id: str
    label: str
    direction: Direction
    size: float
    leg1: SimLeg  # YES leg
    leg2: SimLeg  # NO leg
    gross_edge_per_contract: float
    net_edge_per_contract: float
    created_at: datetime = field(default_factory=utcnow)
    fills: list[SimFill] = field(default_factory=list)

    @property
    def total_fee(self) -> float:
        return self.leg1.fee + self.leg2.fee

    @property
    def total_cost(self) -> float:
        return self.leg1.cost_with_fee + self.leg2.cost_with_fee

    @property
    def expected_pnl(self) -> float:
        """Deterministic PnL at settlement (exactly one leg pays $1/contract)."""
        return self.size * 1.0 - self.total_cost


def _leg_fee(
    fees: FeeEngine, mapping: MarketMapping, exchange: Exchange, avg_price: float, size: float
) -> float:
    return fees.estimate(
        exchange,
        avg_price,
        size,
        category=mapping.category,
        ticker=mapping.kalshi.ticker,
    )


def simulate_trade(
    mapping: MarketMapping,
    opp: ArbOpportunity,
    books: dict[Exchange, MarketBook],
    fees: FeeEngine,
) -> Optional[SimulatedTrade]:
    """Build a :class:`SimulatedTrade` from ``opp`` and the latest ``books``.

    Returns ``None`` if the books can no longer support any fill on both legs.
    """
    yes_ex = opp.leg_yes.exchange
    no_ex = opp.leg_no.exchange
    yes_book = books.get(yes_ex)
    no_book = books.get(no_ex)
    if yes_book is None or no_book is None:
        return None

    yes_asks = yes_book.yes.asks
    no_asks = no_book.no.asks

    # First pass at the requested size to learn how much each leg can fill.
    yes_fills = walk_book(yes_asks, opp.size)
    no_fills = walk_book(no_asks, opp.size)
    yes_filled = sum(f.size for f in yes_fills)
    no_filled = sum(f.size for f in no_fills)
    size = min(yes_filled, no_filled)
    if size <= _EPS:
        return None

    # Re-walk at the common executable size so both legs match exactly.
    if size + _EPS < opp.size:
        yes_fills = walk_book(yes_asks, size)
        no_fills = walk_book(no_asks, size)

    avg_yes = sum(f.price * f.size for f in yes_fills) / sum(f.size for f in yes_fills)
    avg_no = sum(f.price * f.size for f in no_fills) / sum(f.size for f in no_fills)

    fee_yes = _leg_fee(fees, mapping, yes_ex, avg_yes, size)
    fee_no = _leg_fee(fees, mapping, no_ex, avg_no, size)

    gross = 1.0 - (avg_yes + avg_no)
    net = gross - (fee_yes + fee_no) / size

    leg_yes = SimLeg(
        exchange=yes_ex,
        side=Side.YES,
        venue_market_id=opp.leg_yes.venue_market_id,
        avg_price=avg_yes,
        size=size,
        fee=fee_yes,
    )
    leg_no = SimLeg(
        exchange=no_ex,
        side=Side.NO,
        venue_market_id=opp.leg_no.venue_market_id,
        avg_price=avg_no,
        size=size,
        fee=fee_no,
    )

    fills = [
        SimFill(yes_ex, Side.YES, opp.leg_yes.venue_market_id, f.price, f.size)
        for f in yes_fills
    ] + [
        SimFill(no_ex, Side.NO, opp.leg_no.venue_market_id, f.price, f.size)
        for f in no_fills
    ]

    return SimulatedTrade(
        market_id=opp.market_id,
        label=opp.label,
        direction=opp.direction,
        size=size,
        leg1=leg_yes,
        leg2=leg_no,
        gross_edge_per_contract=gross,
        net_edge_per_contract=net,
        fills=fills,
    )
