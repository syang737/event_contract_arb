"""Typed configuration loaded from ``config/exchanges.yaml`` and ``markets.yaml``.

Everything is modeled with pydantic so mis-typed YAML fails loudly at startup
rather than deep inside the poll loop.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .core.fees import FeeEngine, KalshiFeeModel, PolymarketFeeModel
from .core.models import Exchange


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# exchanges.yaml
# --------------------------------------------------------------------------- #
class ProxyConfig(_Base):
    type: Literal["socks5", "socks5h", "http", "https"] = "http"
    url: str

    def httpx_proxy(self) -> str:
        """Return a proxy URL httpx understands (it infers scheme from the URL)."""
        return self.url


class PolymarketFeeConfig(_Base):
    base_rate: float = 0.0
    category_rates: dict[str, float] = Field(default_factory=dict)

    def build(self) -> PolymarketFeeModel:
        return PolymarketFeeModel(base_rate=self.base_rate, category_rates=dict(self.category_rates))


class KalshiFeeConfig(_Base):
    fee_rate: float = 0.07
    per_market_rates: dict[str, float] = Field(default_factory=dict)

    def build(self) -> KalshiFeeModel:
        return KalshiFeeModel(fee_rate=self.fee_rate, per_market_rates=dict(self.per_market_rates))


class PolymarketExchangeConfig(_Base):
    base_url: str = "https://clob.polymarket.com"
    ws_url: Optional[str] = None
    timeout_seconds: float = 10.0
    proxy: Optional[ProxyConfig] = None
    fees: PolymarketFeeConfig = Field(default_factory=PolymarketFeeConfig)


class KalshiExchangeConfig(_Base):
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    ws_url: Optional[str] = None
    env: Literal["prod", "demo"] = "prod"
    timeout_seconds: float = 10.0
    proxy: Optional[ProxyConfig] = None
    api_key_id: Optional[str] = None
    fees: KalshiFeeConfig = Field(default_factory=KalshiFeeConfig)

    @property
    def active_base_url(self) -> str:
        return self.demo_base_url if self.env == "demo" else self.base_url


class ExchangesConfig(_Base):
    polymarket: PolymarketExchangeConfig = Field(default_factory=PolymarketExchangeConfig)
    kalshi: KalshiExchangeConfig = Field(default_factory=KalshiExchangeConfig)


class MarketParams(_Base):
    """Per-market risk / detection parameters (merged over engine defaults)."""

    min_edge_cents: float = 2.0
    min_liquidity: float = 50.0
    max_notional_per_arb: float = 200.0
    min_size: float = 1.0
    size_step: float = 1.0
    min_hours_to_expiry: float = 1.0

    def merged(self, override: Optional["MarketParams | dict"]) -> "MarketParams":
        if override is None:
            return self.model_copy()
        data = self.model_dump()
        override_data = override.model_dump() if isinstance(override, MarketParams) else dict(override)
        data.update({k: v for k, v in override_data.items() if v is not None})
        return MarketParams(**data)


class EngineSettings(_Base):
    poll_interval_seconds: float = 5.0
    safety_buffer: float = 0.005
    staleness_seconds: float = 10.0
    max_total_notional: float = 5000.0
    max_notional_per_exchange: float = 3000.0
    max_open_trades_per_market: int = 1
    db_url: str = "sqlite:///data/arb.db"
    defaults: MarketParams = Field(default_factory=MarketParams)


# --------------------------------------------------------------------------- #
# markets.yaml
# --------------------------------------------------------------------------- #
class PolymarketMarketRef(_Base):
    market_id: str  # CLOB conditionId
    yes_token: str  # ERC-1155 token id for the YES outcome
    no_token: str  # ERC-1155 token id for the NO outcome


class KalshiMarketRef(_Base):
    ticker: str
    yes_side: str = "YES"
    no_side: str = "NO"


class MarketMapping(_Base):
    id: str
    label: str
    category: Optional[str] = None
    close_time: Optional[datetime] = None
    polymarket: PolymarketMarketRef
    kalshi: KalshiMarketRef
    params: Optional[MarketParams] = None


class MarketsConfig(_Base):
    markets: list[MarketMapping] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Combined application config
# --------------------------------------------------------------------------- #
class AppConfig(_Base):
    engine: EngineSettings
    exchanges: ExchangesConfig
    markets: list[MarketMapping]

    def resolved_params(self, mapping: MarketMapping) -> MarketParams:
        """Per-market params with engine defaults filled in."""
        return self.engine.defaults.merged(mapping.params)

    def fee_engine(self) -> FeeEngine:
        return FeeEngine(
            polymarket=self.exchanges.polymarket.fees.build(),
            kalshi=self.exchanges.kalshi.fees.build(),
        )

    def venue_market_id(self, mapping: MarketMapping, exchange: Exchange) -> str:
        if exchange is Exchange.POLYMARKET:
            return mapping.polymarket.market_id
        return mapping.kalshi.ticker


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at the top level of {path}")
    return data


def load_config(
    exchanges_path: str | Path = "config/exchanges.yaml",
    markets_path: str | Path = "config/markets.yaml",
) -> AppConfig:
    """Load and validate both YAML files into a single :class:`AppConfig`."""
    exch_raw = _load_yaml(Path(exchanges_path))
    markets_raw = _load_yaml(Path(markets_path))

    engine = EngineSettings(**exch_raw.get("engine", {}))
    exchanges = ExchangesConfig(**exch_raw.get("exchanges", {}))
    markets_cfg = MarketsConfig(**markets_raw)

    return AppConfig(engine=engine, exchanges=exchanges, markets=markets_cfg.markets)
