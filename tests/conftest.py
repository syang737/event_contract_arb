"""Shared fixtures and builders for the test suite."""

from __future__ import annotations

import pytest

from arb_engine.config import (
    AppConfig,
    EngineSettings,
    ExchangesConfig,
    KalshiExchangeConfig,
    KalshiMarketRef,
    MarketMapping,
    MarketParams,
    PolymarketExchangeConfig,
    PolymarketMarketRef,
)
from arb_engine.core.models import BookLevel, BookSide, Exchange, MarketBook, utcnow


def make_mapping(
    market_id: str = "m1",
    *,
    min_edge_cents: float = 2.0,
    min_liquidity: float = 10.0,
    max_notional_per_arb: float = 1000.0,
    size_step: float = 1.0,
    category: str = "politics",
    close_time=None,
) -> MarketMapping:
    return MarketMapping(
        id=market_id,
        label=f"Market {market_id}",
        category=category,
        close_time=close_time,
        polymarket=PolymarketMarketRef(
            market_id=f"0x{market_id}", yes_token=f"{market_id}_Y", no_token=f"{market_id}_N"
        ),
        kalshi=KalshiMarketRef(ticker=f"{market_id.upper()}-TKR"),
        params=MarketParams(
            min_edge_cents=min_edge_cents,
            min_liquidity=min_liquidity,
            max_notional_per_arb=max_notional_per_arb,
            size_step=size_step,
            min_size=1.0,
            min_hours_to_expiry=0.0,
        ),
    )


def make_config(
    *mappings: MarketMapping,
    safety_buffer: float = 0.005,
    staleness_seconds: float = 30.0,
    max_total_notional: float = 1_000_000.0,
    max_notional_per_exchange: float = 1_000_000.0,
    max_open_trades_per_market: int = 1,
    kalshi_fee_rate: float = 0.07,
) -> AppConfig:
    if not mappings:
        mappings = (make_mapping(),)
    engine = EngineSettings(
        safety_buffer=safety_buffer,
        staleness_seconds=staleness_seconds,
        max_total_notional=max_total_notional,
        max_notional_per_exchange=max_notional_per_exchange,
        max_open_trades_per_market=max_open_trades_per_market,
        db_url="sqlite:///:memory:",
    )
    exchanges = ExchangesConfig(
        polymarket=PolymarketExchangeConfig(),
        kalshi=KalshiExchangeConfig(),
    )
    exchanges.kalshi.fees.fee_rate = kalshi_fee_rate
    return AppConfig(engine=engine, exchanges=exchanges, markets=list(mappings))


def book(
    exchange: Exchange,
    mapping: MarketMapping,
    *,
    yes_asks: list[tuple[float, float]],
    no_asks: list[tuple[float, float]],
    yes_bids: list[tuple[float, float]] | None = None,
    no_bids: list[tuple[float, float]] | None = None,
) -> MarketBook:
    def side(asks, bids) -> BookSide:
        return BookSide(
            asks=[BookLevel(p, s) for p, s in asks],
            bids=[BookLevel(p, s) for p, s in (bids or [])],
        ).sorted()

    return MarketBook(
        exchange=exchange,
        market_id=mapping.id,
        venue_market_id=(
            mapping.polymarket.market_id
            if exchange is Exchange.POLYMARKET
            else mapping.kalshi.ticker
        ),
        yes=side(yes_asks, yes_bids),
        no=side(no_asks, no_bids),
        ts=utcnow(),
    )


@pytest.fixture
def mapping() -> MarketMapping:
    return make_mapping()


@pytest.fixture
def config(mapping) -> AppConfig:
    return make_config(mapping)
