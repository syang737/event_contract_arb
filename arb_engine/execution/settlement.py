"""Settlement: resolve OPEN paper trades and record realized PnL.

Because each trade holds YES on one venue and NO on the other, exactly one leg
pays \\$1/contract at settlement regardless of the outcome, so::

    payout_total  = size * 1.0
    realized_pnl  = payout_total - (leg costs + fees)

We still query a resolution endpoint to confirm the market has actually settled
before booking the PnL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from ..config import MarketMapping
from ..core.models import Exchange, utcnow
from ..exchanges.base import ExchangeClient, ExchangeError, MarketResolution
from ..storage.db import Database, TradeRow


@dataclass
class SettlementResult:
    settled: int = 0
    still_open: int = 0
    errors: int = 0
    realized_pnl: float = 0.0


async def _resolve(
    mapping: MarketMapping, clients: dict[Exchange, ExchangeClient]
) -> Optional[MarketResolution]:
    """Return the first confirmed resolution across venues (they settle the same
    real-world event), or a not-resolved marker, or ``None`` if all calls failed."""
    saw_response = False
    for exchange in (Exchange.KALSHI, Exchange.POLYMARKET):
        client = clients.get(exchange)
        if client is None:
            continue
        try:
            res = await client.get_resolution(mapping)
        except ExchangeError:
            continue
        saw_response = True
        if res.resolved:
            return res
    return MarketResolution(resolved=False) if saw_response else None


async def settle_open_trades(
    db: Database,
    mappings: dict[str, MarketMapping],
    clients: dict[Exchange, ExchangeClient],
) -> SettlementResult:
    result = SettlementResult()
    resolution_cache: dict[str, Optional[MarketResolution]] = {}

    with db.session() as s:
        open_trades = list(s.scalars(select(TradeRow).where(TradeRow.status == "OPEN")))
        for trade in open_trades:
            mapping = mappings.get(trade.market_id)
            if mapping is None:
                result.errors += 1
                result.still_open += 1
                continue

            if trade.market_id not in resolution_cache:
                resolution_cache[trade.market_id] = await _resolve(mapping, clients)
            res = resolution_cache[trade.market_id]

            if res is None:  # all venue calls failed
                result.errors += 1
                result.still_open += 1
                continue
            if not res.resolved:
                result.still_open += 1
                continue

            payout = trade.size * 1.0
            realized = payout - trade.total_cost
            trade.status = "SETTLED"
            trade.payout_total = payout
            trade.realized_pnl = realized
            trade.settlement_price_yes = res.settlement_price_yes
            trade.settled_at = utcnow()

            result.settled += 1
            result.realized_pnl += realized

    return result
