from arb_engine.config import load_config
from arb_engine.core.models import Exchange


def test_load_repo_config():
    cfg = load_config("config/exchanges.yaml", "config/markets.yaml")
    assert cfg.engine.poll_interval_seconds > 0
    assert len(cfg.markets) >= 1

    m = cfg.markets[0]
    # Per-market params merge over engine defaults.
    params = cfg.resolved_params(m)
    assert params.min_edge_cents == 3  # override in markets.yaml
    assert params.min_size == cfg.engine.defaults.min_size  # inherited default

    # Fee engine builds from config.
    fees = cfg.fee_engine()
    assert fees.kalshi.fee_rate == cfg.exchanges.kalshi.fees.fee_rate

    assert cfg.venue_market_id(m, Exchange.KALSHI) == m.kalshi.ticker
    assert cfg.venue_market_id(m, Exchange.POLYMARKET) == m.polymarket.market_id


def test_kalshi_env_switches_base_url():
    cfg = load_config("config/exchanges.yaml", "config/markets.yaml")
    ka = cfg.exchanges.kalshi
    ka.env = "demo"
    assert ka.active_base_url == ka.demo_base_url
    ka.env = "prod"
    assert ka.active_base_url == ka.base_url
