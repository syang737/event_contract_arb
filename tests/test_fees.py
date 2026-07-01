import math

import pytest

from arb_engine.core.fees import FeeEngine, KalshiFeeModel, PolymarketFeeModel
from arb_engine.core.models import Exchange


def test_kalshi_fee_matches_published_formula():
    model = KalshiFeeModel(fee_rate=0.07)
    # fee = ceil(0.07 * C * P * (1-P)) rounded up to the next cent.
    for contracts, price in [(1, 0.5), (100, 0.5), (10, 0.2), (250, 0.65)]:
        raw = 0.07 * contracts * price * (1 - price)
        expected = math.ceil(raw * 100 - 1e-9) / 100.0
        assert model.estimate(price, contracts) == expected


def test_kalshi_fee_rounds_up_to_cent():
    model = KalshiFeeModel(fee_rate=0.07)
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounds up to $0.02.
    assert model.estimate(0.5, 1) == 0.02


def test_kalshi_per_market_rate_override_prefix():
    model = KalshiFeeModel(fee_rate=0.07, per_market_rates={"SP500": 0.035})
    assert model.rate_for("SP500-26DEC31-B5000") == 0.035
    assert model.rate_for("PRES-2028") == 0.07


def test_polymarket_fee_symmetric_around_half():
    model = PolymarketFeeModel(base_rate=0.02)
    # min(p, 1-p) makes 0.3 and 0.7 identical.
    assert model.estimate(0.3, 100) == pytest.approx(model.estimate(0.7, 100))
    assert model.estimate(0.3, 100) == pytest.approx(0.02 * 0.3 * 100)


def test_polymarket_zero_default():
    model = PolymarketFeeModel()
    assert model.estimate(0.5, 1000) == 0.0


def test_fee_engine_dispatch():
    engine = FeeEngine(
        polymarket=PolymarketFeeModel(base_rate=0.01),
        kalshi=KalshiFeeModel(fee_rate=0.07),
    )
    assert engine.estimate(Exchange.POLYMARKET, 0.4, 100) == 0.01 * 0.4 * 100
    assert engine.estimate(Exchange.KALSHI, 0.5, 1) == 0.02
