from dataclasses import replace
from datetime import timedelta

from arb_engine.core.arbitrage import ArbDetector
from arb_engine.core.models import Direction, Exchange, Side, utcnow

from .conftest import book, make_config, make_mapping


def _pm(mapping, **kw):
    return book(Exchange.POLYMARKET, mapping, **kw)


def _ka(mapping, **kw):
    return book(Exchange.KALSHI, mapping, **kw)


def test_detects_d1_arb_and_optimizes_size(config, mapping):
    pm = _pm(mapping, yes_asks=[(0.44, 100)], no_asks=[(0.60, 100)])
    ka = _ka(mapping, yes_asks=[(0.62, 100)], no_asks=[(0.48, 100)])

    opps = ArbDetector(config).detect_market(mapping, pm, ka)
    assert len(opps) == 1
    opp = opps[0]
    assert opp.direction is Direction.YES_PM_NO_KA
    assert opp.leg_yes.exchange is Exchange.POLYMARKET
    assert opp.leg_yes.side is Side.YES
    assert opp.leg_no.exchange is Exchange.KALSHI
    assert opp.leg_no.side is Side.NO
    # Size optimization consumes the full available depth (100).
    assert opp.size == 100
    assert opp.gross_edge_per_contract > 0.079
    assert opp.net_edge_per_contract_adj >= 0.02
    assert opp.total_net_profit > 0


def test_detects_d2_direction(config, mapping):
    # Cheap YES on Kalshi + cheap NO on Polymarket.
    pm = _pm(mapping, yes_asks=[(0.62, 100)], no_asks=[(0.44, 100)])
    ka = _ka(mapping, yes_asks=[(0.48, 100)], no_asks=[(0.60, 100)])

    opps = ArbDetector(config).detect_market(mapping, pm, ka)
    assert len(opps) == 1
    assert opps[0].direction is Direction.YES_KA_NO_PM
    assert opps[0].leg_yes.exchange is Exchange.KALSHI
    assert opps[0].leg_no.exchange is Exchange.POLYMARKET


def test_no_arb_when_sum_above_one(config, mapping):
    pm = _pm(mapping, yes_asks=[(0.55, 100)], no_asks=[(0.55, 100)])
    ka = _ka(mapping, yes_asks=[(0.56, 100)], no_asks=[(0.56, 100)])
    assert ArbDetector(config).detect_market(mapping, pm, ka) == []


def test_edge_below_threshold_is_filtered(mapping):
    # Gross edge ~1c, below the 5c min_edge threshold.
    cfg = make_config(make_mapping(min_edge_cents=5.0, min_liquidity=10.0))
    m = cfg.markets[0]
    pm = _pm(m, yes_asks=[(0.49, 100)], no_asks=[(0.60, 100)])
    ka = _ka(m, yes_asks=[(0.62, 100)], no_asks=[(0.50, 100)])
    assert ArbDetector(cfg).detect_market(m, pm, ka) == []


def test_liquidity_filter(mapping):
    cfg = make_config(make_mapping(min_liquidity=500.0))
    m = cfg.markets[0]
    pm = _pm(m, yes_asks=[(0.44, 100)], no_asks=[(0.60, 100)])
    ka = _ka(m, yes_asks=[(0.62, 100)], no_asks=[(0.48, 100)])
    assert ArbDetector(cfg).detect_market(m, pm, ka) == []


def test_staleness_filter(config, mapping):
    pm = _pm(mapping, yes_asks=[(0.44, 100)], no_asks=[(0.60, 100)])
    ka = _ka(mapping, yes_asks=[(0.62, 100)], no_asks=[(0.48, 100)])
    stale_pm = replace(pm, ts=utcnow() - timedelta(seconds=120))
    assert ArbDetector(config).detect_market(mapping, stale_pm, ka) == []


def test_time_to_expiry_filter():
    m = make_mapping(min_liquidity=10.0, close_time=utcnow() + timedelta(minutes=30))
    m.params.min_hours_to_expiry = 2.0  # 30 min < 2h -> filtered
    cfg = make_config(m)
    pm = _pm(m, yes_asks=[(0.44, 100)], no_asks=[(0.60, 100)])
    ka = _ka(m, yes_asks=[(0.62, 100)], no_asks=[(0.48, 100)])
    assert ArbDetector(cfg).detect_market(m, pm, ka) == []


def test_notional_cap_limits_size(mapping):
    # Cap arb notional at $50 so size can't grow to full depth.
    cfg = make_config(make_mapping(min_liquidity=10.0, max_notional_per_arb=50.0))
    m = cfg.markets[0]
    pm = _pm(m, yes_asks=[(0.44, 1000)], no_asks=[(0.60, 1000)])
    ka = _ka(m, yes_asks=[(0.62, 1000)], no_asks=[(0.48, 1000)])
    opp = ArbDetector(cfg).detect_market(m, pm, ka)[0]
    assert opp.total_cost <= 50.0 + 1e-6
    assert opp.size < 1000
