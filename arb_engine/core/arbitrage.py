"""Cross-exchange arbitrage detection with size optimization.

For every mapped market and the two candidate directions we walk the two books,
model fees and a safety buffer, and search discrete sizes for the one with the
highest expected net profit that still clears the per-contract edge threshold.

The core identity: buying YES on one venue and NO on the other for a combined
cost below \\$1/contract locks a profit, because at settlement exactly one leg
pays \\$1. Net edge per contract is therefore::

    gross = 1 - (avg_yes + avg_no)
    net   = gross - (fee_yes + fee_no) / size
    adj   = net - safety_buffer
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator, Optional

from ..config import AppConfig, MarketMapping, MarketParams
from .fees import FeeEngine
from .models import (
    ArbLeg,
    ArbOpportunity,
    BookSide,
    Direction,
    Exchange,
    MarketBook,
    Side,
    utcnow,
)
from .orderbook import simulate_fill_from_book

_EPS = 1e-9
_MAX_SIZE_ITERATIONS = 10_000


def _candidate_sizes(params: MarketParams) -> Iterator[float]:
    """Yield ascending target sizes to probe, bounded for safety."""
    step = params.size_step if params.size_step > 0 else params.min_size
    size = params.min_size
    for _ in range(_MAX_SIZE_ITERATIONS):
        yield size
        if step <= 0:
            return
        size += step


class ArbDetector:
    """Stateless detector: turns paired books into ranked opportunities."""

    def __init__(self, config: AppConfig, fee_engine: Optional[FeeEngine] = None):
        self.config = config
        self.fees = fee_engine or config.fee_engine()

    # ------------------------------------------------------------------ #
    def detect_market(
        self,
        mapping: MarketMapping,
        pm_book: MarketBook,
        ka_book: MarketBook,
        now: Optional[datetime] = None,
    ) -> list[ArbOpportunity]:
        """Return the best opportunity per direction (0, 1, or 2 total)."""
        now = now or utcnow()
        params = self.config.resolved_params(mapping)

        # Staleness filter: never trade on old snapshots.
        stale_s = self.config.engine.staleness_seconds
        if pm_book.is_stale(stale_s, now) or ka_book.is_stale(stale_s, now):
            return []

        # Time-to-expiry filter.
        if self._too_close_to_expiry(mapping, params, now):
            return []

        opportunities: list[ArbOpportunity] = []

        # D1: buy YES on Polymarket, buy NO on Kalshi.
        d1 = self._evaluate_direction(
            mapping,
            params,
            direction=Direction.YES_PM_NO_KA,
            yes_exchange=Exchange.POLYMARKET,
            yes_side=pm_book.yes,
            yes_venue_id=mapping.polymarket.yes_token,
            no_exchange=Exchange.KALSHI,
            no_side=ka_book.no,
            no_venue_id=mapping.kalshi.ticker,
        )
        if d1 is not None:
            opportunities.append(d1)

        # D2: buy YES on Kalshi, buy NO on Polymarket.
        d2 = self._evaluate_direction(
            mapping,
            params,
            direction=Direction.YES_KA_NO_PM,
            yes_exchange=Exchange.KALSHI,
            yes_side=ka_book.yes,
            yes_venue_id=mapping.kalshi.ticker,
            no_exchange=Exchange.POLYMARKET,
            no_side=pm_book.no,
            no_venue_id=mapping.polymarket.no_token,
        )
        if d2 is not None:
            opportunities.append(d2)

        return opportunities

    def detect(
        self,
        books: dict[str, dict[Exchange, MarketBook]],
        now: Optional[datetime] = None,
    ) -> list[ArbOpportunity]:
        """Detect across all mapped markets given ``{market_id: {exchange: book}}``."""
        now = now or utcnow()
        out: list[ArbOpportunity] = []
        for mapping in self.config.markets:
            venue_books = books.get(mapping.id)
            if not venue_books:
                continue
            pm_book = venue_books.get(Exchange.POLYMARKET)
            ka_book = venue_books.get(Exchange.KALSHI)
            if pm_book is None or ka_book is None:
                continue
            out.extend(self.detect_market(mapping, pm_book, ka_book, now))
        # Rank most profitable first.
        out.sort(key=lambda o: o.total_net_profit, reverse=True)
        return out

    # ------------------------------------------------------------------ #
    def _too_close_to_expiry(
        self, mapping: MarketMapping, params: MarketParams, now: datetime
    ) -> bool:
        if mapping.close_time is None or params.min_hours_to_expiry <= 0:
            return False
        close = mapping.close_time
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        hours_left = (close - now).total_seconds() / 3600.0
        return hours_left < params.min_hours_to_expiry

    def _evaluate_direction(
        self,
        mapping: MarketMapping,
        params: MarketParams,
        *,
        direction: Direction,
        yes_exchange: Exchange,
        yes_side: BookSide,
        yes_venue_id: str,
        no_exchange: Exchange,
        no_side: BookSide,
        no_venue_id: str,
    ) -> Optional[ArbOpportunity]:
        """Size-optimize a single direction; return best opportunity or None."""
        if not yes_side.asks or not no_side.asks:
            return None

        # Liquidity filter: require cumulative ask depth on both legs.
        if yes_side.ask_depth < params.min_liquidity - _EPS:
            return None
        if no_side.ask_depth < params.min_liquidity - _EPS:
            return None

        threshold = params.min_edge_cents / 100.0
        ticker = mapping.kalshi.ticker
        category = mapping.category

        best: Optional[ArbOpportunity] = None
        for size in _candidate_sizes(params):
            avg_yes, filled_yes = simulate_fill_from_book(yes_side.asks, size)
            avg_no, filled_no = simulate_fill_from_book(no_side.asks, size)

            # Require a *full* fill on both legs; since depth is monotone, once a
            # size can't be filled no larger size can either.
            if filled_yes + _EPS < size or filled_no + _EPS < size:
                break

            fee_yes = self.fees.estimate(
                yes_exchange, avg_yes, size, category=category, ticker=ticker
            )
            fee_no = self.fees.estimate(
                no_exchange, avg_no, size, category=category, ticker=ticker
            )

            total_cost = avg_yes * size + avg_no * size + fee_yes + fee_no
            # Notional cap: cost is monotone in size, so stop once exceeded.
            if total_cost > params.max_notional_per_arb + _EPS:
                break

            gross = 1.0 - (avg_yes + avg_no)
            net = gross - (fee_yes + fee_no) / size
            net_adj = net - self.config.engine.safety_buffer

            if net_adj + _EPS < threshold:
                continue

            candidate = ArbOpportunity(
                market_id=mapping.id,
                label=mapping.label,
                direction=direction,
                size=size,
                leg_yes=ArbLeg(
                    exchange=yes_exchange,
                    side=Side.YES,
                    venue_market_id=yes_venue_id,
                    avg_price=avg_yes,
                    size=size,
                    fee=fee_yes,
                ),
                leg_no=ArbLeg(
                    exchange=no_exchange,
                    side=Side.NO,
                    venue_market_id=no_venue_id,
                    avg_price=avg_no,
                    size=size,
                    fee=fee_no,
                ),
                gross_edge_per_contract=gross,
                net_edge_per_contract=net,
                net_edge_per_contract_adj=net_adj,
            )
            if best is None or candidate.total_net_profit > best.total_net_profit:
                best = candidate

        return best
