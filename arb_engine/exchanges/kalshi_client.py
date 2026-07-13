"""Kalshi Predictions (event-contract) market-data client.

Talks to the Kalshi trade-api v2 REST endpoints:

* ``GET /markets/<ticker>/orderbook`` – resting depth.
* ``GET /markets/<ticker>``           – metadata incl. status / result.

Kalshi quotes integer **cents** (1..99), which we divide by 100 to dollars.
Critically, its order book only exposes *resting bids* on each side::

    {"orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}

A resting NO bid at ``p`` cents is an offer to sell YES at ``100 - p`` cents, so
we derive the YES **ask** side from the NO bids (and vice-versa). This client
normalizes both sides into :class:`MarketBook` asks/bids so downstream code
treats Kalshi and Polymarket identically.

Read-only market data is public on prod. Signed (RSA-PSS) order placement is out
of scope for the paper-trading build; ``api_key_id`` is accepted but unused here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import KalshiExchangeConfig, MarketMapping
from ..core.models import BookLevel, BookSide, Exchange, MarketBook, utcnow
from ..mapping.models import VenueMarket
from .base import (
    ExchangeClient,
    ExchangeError,
    MarketResolution,
    RateLimitedError,
)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

# Kalshi statuses that indicate a terminal, settled market.
_SETTLED_STATUSES = {"settled", "finalized", "determined", "closed"}


class KalshiClient(ExchangeClient):
    exchange = Exchange.KALSHI

    def __init__(
        self,
        config: Optional[KalshiExchangeConfig] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.config = config or KalshiExchangeConfig()
        self._owns_client = client is None
        self._client = client or self._build_client()

    def _build_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self.config.active_base_url,
            "timeout": self.config.timeout_seconds,
            "headers": {"Accept": "application/json"},
        }
        if self.config.proxy is not None:
            kwargs["proxy"] = self.config.proxy.httpx_proxy()
        return httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    async def _get(self, url: str, params: Optional[dict] = None) -> Any:
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ExchangeError(f"Kalshi request failed: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitedError("Kalshi rate limit hit (HTTP 429).")
        if resp.status_code >= 400:
            raise ExchangeError(
                f"Kalshi HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        return resp.json()

    @staticmethod
    def _to_levels(raw: Optional[list], invert: bool) -> list[BookLevel]:
        """Convert Kalshi ``[[cents, qty], ...]`` bids into normalized levels.

        When ``invert`` is True the levels represent the *opposite* outcome's
        resting bids, so a bid at ``p`` cents becomes an ask at ``(100 - p)/100``
        dollars for the outcome we care about.
        """
        levels: list[BookLevel] = []
        for entry in raw or []:
            try:
                cents = float(entry[0])
                qty = float(entry[1])
            except (IndexError, TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            price = (100.0 - cents) / 100.0 if invert else cents / 100.0
            levels.append(BookLevel(price=price, size=qty))
        return levels

    async def fetch_book(self, mapping: MarketMapping) -> MarketBook:
        ticker = mapping.kalshi.ticker
        data = await self._get(f"/markets/{ticker}/orderbook")
        ob = data.get("orderbook", {}) or {}
        yes_bids_raw = ob.get("yes")
        no_bids_raw = ob.get("no")

        # YES side: bids come straight from `yes`; asks are derived from NO bids.
        yes_side = BookSide(
            bids=self._to_levels(yes_bids_raw, invert=False),
            asks=self._to_levels(no_bids_raw, invert=True),
        ).sorted()
        # NO side: bids from `no`; asks derived from YES bids.
        no_side = BookSide(
            bids=self._to_levels(no_bids_raw, invert=False),
            asks=self._to_levels(yes_bids_raw, invert=True),
        ).sorted()

        return MarketBook(
            exchange=Exchange.KALSHI,
            market_id=mapping.id,
            venue_market_id=ticker,
            yes=yes_side,
            no=no_side,
            ts=utcnow(),
        )

    async def list_markets(
        self, *, page_size: int = 1000, max_pages: int = 40, status: str = "open"
    ) -> list[VenueMarket]:
        """Discover markets via trade-api v2 ``/markets`` (cursor-paginated)."""
        out: list[VenueMarket] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            markets = data.get("markets", []) or []
            for raw in markets:
                vm = self._parse_market_meta(raw)
                if vm is not None:
                    out.append(vm)
            cursor = data.get("cursor") or None
            if not cursor or not markets:
                break
        return out

    @staticmethod
    def _parse_market_meta(raw: dict) -> Optional[VenueMarket]:
        ticker = raw.get("ticker")
        if not ticker:
            return None
        strike_type = raw.get("strike_type")
        floor_strike = raw.get("floor_strike")
        cap_strike = raw.get("cap_strike")
        # Pick the bounding strike that defines the YES threshold.
        strike = floor_strike if floor_strike is not None else cap_strike
        strike_cap = cap_strike if strike_type == "between" else None

        title = raw.get("title", "") or ""
        subtitle = raw.get("subtitle") or raw.get("yes_sub_title") or ""
        full_title = f"{title} {subtitle}".strip() if subtitle else title

        return VenueMarket(
            exchange=Exchange.KALSHI,
            venue_id=str(ticker),
            title=full_title,
            description=raw.get("yes_sub_title", "") or raw.get("rules_primary", "") or "",
            category=raw.get("category"),
            close_time=_parse_iso(raw.get("close_time")),
            status=str(raw.get("status", "active")),
            strike_type=strike_type,
            strike=float(strike) if isinstance(strike, (int, float)) else None,
            strike_cap=float(strike_cap) if isinstance(strike_cap, (int, float)) else None,
            event_key=raw.get("event_ticker"),
            series_key=raw.get("series_ticker"),
            raw=raw,
        )

    async def get_resolution(self, mapping: MarketMapping) -> MarketResolution:
        data = await self._get(f"/markets/{mapping.kalshi.ticker}")
        market = data.get("market", data) or {}
        result = str(market.get("result", "")).lower()
        status = str(market.get("status", "")).lower()

        if result == "yes":
            return MarketResolution(resolved=True, yes_won=True)
        if result == "no":
            return MarketResolution(resolved=True, yes_won=False)
        # Terminal status but no explicit result recorded yet.
        if status in _SETTLED_STATUSES and result:
            return MarketResolution(resolved=True, yes_won=None)
        return MarketResolution(resolved=False)
