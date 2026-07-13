"""Market-data clients (Polymarket CLOB, Kalshi Predictions, and a mock)."""

from .base import ExchangeClient, MarketResolution
from .kalshi_client import KalshiClient
from .mock_client import MockClient
from .polymarket_client import PolymarketClient

__all__ = [
    "ExchangeClient",
    "MarketResolution",
    "KalshiClient",
    "MockClient",
    "PolymarketClient",
]
