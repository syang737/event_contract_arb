"""Per-exchange fee estimation.

Both models are intentionally simple and config-driven so v1 can run with
reasonable defaults and be refined later against official fee schedules / SDK
parameters without touching the arbitrage math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .models import Exchange


@dataclass
class PolymarketFeeModel:
    """Taker fee symmetric around 50% probability.

    ``fee = rate * min(price, 1 - price) * size`` where ``rate`` is resolved per
    category (falling back to ``base_rate``). Polymarket historically charged 0,
    so the default rate is 0; set ``category_rates`` as fees roll out per docs.
    """

    base_rate: float = 0.0
    category_rates: dict[str, float] = field(default_factory=dict)

    def rate_for(self, category: Optional[str]) -> float:
        if category is not None and category in self.category_rates:
            return self.category_rates[category]
        return self.base_rate

    def estimate(self, price: float, size: float, category: Optional[str] = None) -> float:
        rate = self.rate_for(category)
        if rate <= 0 or size <= 0:
            return 0.0
        return rate * min(price, 1.0 - price) * size


@dataclass
class KalshiFeeModel:
    """Kalshi general trading fee.

    ``fee = ceil(rate * C * P * (1 - P))`` rounded **up to the next cent**, where
    ``C`` = contracts and ``P`` = price in dollars (0..1). The default rate 0.07
    matches Kalshi's published general fee; specific markets (e.g. S&P/Nasdaq)
    use 0.035 and can be overridden via ``per_market_rates``.
    """

    fee_rate: float = 0.07
    per_market_rates: dict[str, float] = field(default_factory=dict)

    def rate_for(self, ticker: Optional[str]) -> float:
        if ticker is not None:
            # Allow prefix matches so "SP500" covers "SP500-26DEC31-..." style tickers.
            for key, rate in self.per_market_rates.items():
                if ticker == key or ticker.startswith(key):
                    return rate
        return self.fee_rate

    def estimate(self, price: float, size: float, ticker: Optional[str] = None) -> float:
        rate = self.rate_for(ticker)
        if rate <= 0 or size <= 0:
            return 0.0
        raw = rate * size * price * (1.0 - price)
        # Round up to the next whole cent, per Kalshi's fee schedule.
        return math.ceil(raw * 100.0 - 1e-9) / 100.0


@dataclass
class FeeEngine:
    """Dispatches to the correct venue fee model."""

    polymarket: PolymarketFeeModel = field(default_factory=PolymarketFeeModel)
    kalshi: KalshiFeeModel = field(default_factory=KalshiFeeModel)

    def estimate(
        self,
        exchange: Exchange,
        price: float,
        size: float,
        *,
        category: Optional[str] = None,
        ticker: Optional[str] = None,
    ) -> float:
        if exchange is Exchange.POLYMARKET:
            return self.polymarket.estimate(price, size, category=category)
        if exchange is Exchange.KALSHI:
            return self.kalshi.estimate(price, size, ticker=ticker)
        raise ValueError(f"Unknown exchange: {exchange!r}")
