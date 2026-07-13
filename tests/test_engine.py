import pytest

from arb_engine.core.models import Exchange
from arb_engine.engine import ArbEngine, RiskState
from arb_engine.exchanges.base import MarketResolution
from arb_engine.exchanges.mock_client import build_demo_clients
from arb_engine.execution.settlement import settle_open_trades
from arb_engine.storage.db import Database

from .conftest import make_config, make_mapping


def _engine():
    cfg = make_config(make_mapping(min_edge_cents=3.0, min_liquidity=50.0, max_notional_per_arb=200.0))
    db = Database("sqlite:///:memory:")
    pm, ka = build_demo_clients(cfg)
    return cfg, db, pm, ka, ArbEngine(cfg, db, pm, ka)


async def test_poll_once_records_a_paper_trade():
    cfg, db, pm, ka, engine = _engine()
    report = await engine.poll_once()

    assert report.arbs_detected >= 1
    assert report.trades_executed == 1
    trades = db.all_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t.status == "OPEN"
    assert t.size > 0
    # YES leg on Polymarket, NO leg on Kalshi for the demo arb.
    assert t.leg1_exchange == Exchange.POLYMARKET.value
    assert t.leg2_exchange == Exchange.KALSHI.value
    # Per-level fills were persisted.
    assert len(t.fills) >= 2


async def test_max_open_trades_per_market_blocks_duplicates():
    cfg, db, pm, ka, engine = _engine()
    await engine.poll_once()
    await engine.poll_once()  # second cycle should be blocked by the per-market cap
    assert len(db.all_trades()) == 1


async def test_risk_state_reconstructs_from_db():
    cfg, db, pm, ka, engine = _engine()
    await engine.poll_once()
    rebuilt = RiskState.from_db(db)
    assert rebuilt.total_notional > 0
    assert rebuilt.exchange_notional[Exchange.POLYMARKET] > 0
    assert rebuilt.exchange_notional[Exchange.KALSHI] > 0
    assert rebuilt.open_counts


async def test_settlement_books_expected_pnl():
    cfg, db, pm, ka, engine = _engine()
    await engine.poll_once()
    trade = db.all_trades()[0]
    expected = trade.size - trade.total_cost  # deterministic settlement PnL

    for m in cfg.markets:
        pm.set_resolution(m.id, MarketResolution(resolved=True, yes_won=True))
        ka.set_resolution(m.id, MarketResolution(resolved=True, yes_won=True))

    mappings = {m.id: m for m in cfg.markets}
    clients = {Exchange.POLYMARKET: pm, Exchange.KALSHI: ka}
    result = await settle_open_trades(db, mappings, clients)

    assert result.settled == 1
    assert result.still_open == 0
    assert abs(result.realized_pnl - expected) < 1e-9

    settled = db.all_trades()[0]
    assert settled.status == "SETTLED"
    assert settled.payout_total == settled.size
    assert abs(settled.realized_pnl - expected) < 1e-9


async def test_unresolved_market_stays_open():
    cfg, db, pm, ka, engine = _engine()
    await engine.poll_once()
    mappings = {m.id: m for m in cfg.markets}
    clients = {Exchange.POLYMARKET: pm, Exchange.KALSHI: ka}
    result = await settle_open_trades(db, mappings, clients)  # no resolutions set
    assert result.settled == 0
    assert result.still_open == 1
    assert db.all_trades()[0].status == "OPEN"
