"""Async polling orchestration: fetch -> normalize -> detect -> simulate -> store.

Also owns the runtime risk state (capital usage, per-exchange exposure, open
trades per market) that gates whether a detected opportunity becomes a paper
trade.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import AppConfig, MarketMapping
from .core.arbitrage import ArbDetector
from .core.fees import FeeEngine
from .core.models import ArbOpportunity, Exchange, MarketBook
from .exchanges.base import ExchangeClient
from .exchanges.kalshi_client import KalshiClient
from .exchanges.mock_client import build_demo_clients
from .exchanges.polymarket_client import PolymarketClient
from .execution.simulator import SimulatedTrade, simulate_trade
from .storage.db import Database

log = logging.getLogger("arb_engine.engine")


# --------------------------------------------------------------------------- #
@dataclass
class RiskState:
    """Live exposure snapshot used by the capital / concentration filters."""

    total_notional: float = 0.0
    exchange_notional: dict[Exchange, float] = field(
        default_factory=lambda: {Exchange.POLYMARKET: 0.0, Exchange.KALSHI: 0.0}
    )
    open_counts: dict[str, int] = field(default_factory=dict)

    def add_trade(self, sim: SimulatedTrade) -> None:
        self.total_notional += sim.total_cost
        self.exchange_notional[sim.leg1.exchange] += sim.leg1.cost
        self.exchange_notional[sim.leg2.exchange] += sim.leg2.cost
        self.open_counts[sim.market_id] = self.open_counts.get(sim.market_id, 0) + 1

    @classmethod
    def from_db(cls, db: Database) -> "RiskState":
        state = cls()
        for row in db.open_trades():
            state.total_notional += row.total_cost
            for exch, price in (
                (Exchange(row.leg1_exchange), row.leg1_avg_price),
                (Exchange(row.leg2_exchange), row.leg2_avg_price),
            ):
                state.exchange_notional[exch] = (
                    state.exchange_notional.get(exch, 0.0) + price * row.size
                )
            state.open_counts[row.market_id] = state.open_counts.get(row.market_id, 0) + 1
        return state


@dataclass
class GateDecision:
    ok: bool
    reason: str = ""


@dataclass
class PollReport:
    books_fetched: int = 0
    fetch_errors: int = 0
    arbs_detected: int = 0
    trades_executed: int = 0
    skipped: int = 0


# --------------------------------------------------------------------------- #
def build_clients(
    config: AppConfig, *, mock: bool = False
) -> tuple[ExchangeClient, ExchangeClient]:
    """Return a (polymarket, kalshi) client pair (real or in-memory mock)."""
    if mock:
        return build_demo_clients(config)
    pm = PolymarketClient(config.exchanges.polymarket)
    ka = KalshiClient(config.exchanges.kalshi)
    return pm, ka


class ArbEngine:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        pm_client: ExchangeClient,
        ka_client: ExchangeClient,
        *,
        fee_engine: Optional[FeeEngine] = None,
        sample_quotes: bool = False,
    ):
        self.config = config
        self.db = db
        self.pm = pm_client
        self.ka = ka_client
        self.fees = fee_engine or config.fee_engine()
        self.detector = ArbDetector(config, self.fees)
        self.sample_quotes = sample_quotes
        self._mappings: dict[str, MarketMapping] = {m.id: m for m in config.markets}
        self.state = RiskState.from_db(db)

    # ------------------------------------------------------------------ #
    async def _fetch_all(self) -> tuple[dict[str, dict[Exchange, MarketBook]], int]:
        async def fetch(mapping: MarketMapping, client: ExchangeClient, exch: Exchange):
            try:
                book = await client.fetch_book(mapping)
                return mapping.id, exch, book, None
            except Exception as exc:  # noqa: BLE001 - surfaced as per-market error
                return mapping.id, exch, None, exc

        tasks = []
        for mapping in self.config.markets:
            tasks.append(fetch(mapping, self.pm, Exchange.POLYMARKET))
            tasks.append(fetch(mapping, self.ka, Exchange.KALSHI))
        results = await asyncio.gather(*tasks)

        books: dict[str, dict[Exchange, MarketBook]] = {}
        errors = 0
        for market_id, exch, book, err in results:
            if err is not None:
                errors += 1
                log.warning("fetch failed for %s/%s: %s", market_id, exch.value, err)
                continue
            books.setdefault(market_id, {})[exch] = book
            if self.sample_quotes:
                self.db.record_quote_snapshot(book)
        return books, errors

    def _gate(self, opp: ArbOpportunity) -> GateDecision:
        eng = self.config.engine
        open_here = self.state.open_counts.get(opp.market_id, 0)
        if open_here >= eng.max_open_trades_per_market:
            return GateDecision(False, f"max open trades per market reached ({open_here})")

        if self.state.total_notional + opp.total_cost > eng.max_total_notional + 1e-9:
            return GateDecision(False, "would exceed max_total_notional")

        for leg in (opp.leg_yes, opp.leg_no):
            projected = self.state.exchange_notional.get(leg.exchange, 0.0) + leg.cost
            if projected > eng.max_notional_per_exchange + 1e-9:
                return GateDecision(
                    False, f"would exceed max_notional_per_exchange on {leg.exchange.value}"
                )
        return GateDecision(True)

    async def poll_once(self) -> PollReport:
        report = PollReport()
        books, report.fetch_errors = await self._fetch_all()
        report.books_fetched = sum(len(v) for v in books.values())

        opportunities = self.detector.detect(books)
        report.arbs_detected = len(opportunities)

        for opp in opportunities:
            gate = self._gate(opp)
            if not gate.ok:
                self.db.record_arb(opp, executed=False)
                report.skipped += 1
                log.debug("skip arb %s %s: %s", opp.market_id, opp.direction.value, gate.reason)
                continue

            mapping = self._mappings[opp.market_id]
            sim = simulate_trade(mapping, opp, books[opp.market_id], self.fees)
            if sim is None or sim.size <= 0:
                self.db.record_arb(opp, executed=False)
                report.skipped += 1
                continue

            trade_id = self.db.insert_trade(sim)
            self.db.record_arb(opp, executed=True, trade_id=trade_id)
            self.state.add_trade(sim)
            report.trades_executed += 1
            log.info(
                "PAPER TRADE #%d %s %s size=%.2f net_edge=%.4f exp_pnl=%.2f",
                trade_id,
                opp.market_id,
                opp.direction.value,
                sim.size,
                sim.net_edge_per_contract,
                sim.expected_pnl,
            )
        return report

    async def run(self, *, max_iterations: Optional[int] = None) -> None:
        """Poll forever (or ``max_iterations`` times) at the configured interval."""
        self.db.sync_markets(self.config)
        interval = self.config.engine.poll_interval_seconds
        iteration = 0
        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1
                report = await self.poll_once()
                log.info(
                    "cycle %d: books=%d errors=%d arbs=%d trades=%d skipped=%d",
                    iteration,
                    report.books_fetched,
                    report.fetch_errors,
                    report.arbs_detected,
                    report.trades_executed,
                    report.skipped,
                )
                if max_iterations is not None and iteration >= max_iterations:
                    break
                await asyncio.sleep(interval)
        except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
            log.info("poll loop interrupted; shutting down")

    async def aclose(self) -> None:
        await asyncio.gather(self.pm.close(), self.ka.close(), return_exceptions=True)
