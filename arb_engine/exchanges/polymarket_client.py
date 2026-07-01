"""Polymarket CLOB market-data client.

Talks to the CLOB REST API (``https://clob.polymarket.com``):

* ``GET /book?token_id=<id>``       – full depth for one outcome token.
* ``GET /markets/<condition_id>``   – metadata incl. resolution / winner flags.

Polymarket quotes prices in dollars already (``0..1``). Each market has two
ERC-1155 outcome tokens (YES / NO); the book is per-token, so we fetch both and
assemble a normalized :class:`MarketBook`. Geo-block (HTTP 403) and rate-limit
(HTTP 429) responses are surfaced as hard errors, per the spec.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from ..config import MarketMapping, PolymarketExchangeConfig
from ..core.models import BookLevel, BookSide, Exchange, MarketBook, utcnow
from .base import (
    ExchangeClient,
    ExchangeError,
    GeoBlockedError,
    MarketResolution,
    RateLimitedError,
)


class PolymarketClient(ExchangeClient):
    exchange = Exchange.POLYMARKET

    def __init__(
        self,
        config: Optional[PolymarketExchangeConfig] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.config = config or PolymarketExchangeConfig()
        self._owns_client = client is None
        self._client = client or self._build_client()

    def _build_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self.config.base_url,
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
        except httpx.HTTPError as exc:  # network/transport failures
            raise ExchangeError(f"Polymarket request failed: {exc}") from exc

        if resp.status_code == 403:
            raise GeoBlockedError(
                "Polymarket returned 403 (geo-block or auth). Configure a proxy "
                "in exchanges.yaml for geo-restricted regions."
            )
        if resp.status_code == 429:
            raise RateLimitedError("Polymarket rate limit hit (HTTP 429).")
        if resp.status_code >= 400:
            raise ExchangeError(
                f"Polymarket HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        return resp.json()

    @staticmethod
    def _parse_side(levels: list[dict]) -> list[BookLevel]:
        out: list[BookLevel] = []
        for lvl in levels:
            try:
                price = float(lvl["price"])
                size = float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                out.append(BookLevel(price=price, size=size))
        return out

    async def _fetch_token_book(self, token_id: str) -> BookSide:
        data = await self._get("/book", params={"token_id": token_id})
        bids = self._parse_side(data.get("bids", []))
        asks = self._parse_side(data.get("asks", []))
        return BookSide(bids=bids, asks=asks).sorted()

    async def fetch_book(self, mapping: MarketMapping) -> MarketBook:
        yes_side, no_side = await asyncio.gather(
            self._fetch_token_book(mapping.polymarket.yes_token),
            self._fetch_token_book(mapping.polymarket.no_token),
        )
        return MarketBook(
            exchange=Exchange.POLYMARKET,
            market_id=mapping.id,
            venue_market_id=mapping.polymarket.market_id,
            yes=yes_side,
            no=no_side,
            ts=utcnow(),
        )

    async def get_resolution(self, mapping: MarketMapping) -> MarketResolution:
        data = await self._get(f"/markets/{mapping.polymarket.market_id}")
        closed = bool(data.get("closed", False))
        tokens = data.get("tokens", []) or []
        if not closed:
            return MarketResolution(resolved=False)

        yes_won: Optional[bool] = None
        for tok in tokens:
            token_id = str(tok.get("token_id", ""))
            winner = bool(tok.get("winner", False))
            if token_id == mapping.polymarket.yes_token:
                yes_won = winner
            elif token_id == mapping.polymarket.no_token and winner:
                yes_won = False
        return MarketResolution(resolved=True, yes_won=yes_won)
