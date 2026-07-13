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
import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import MarketMapping, PolymarketExchangeConfig
from ..core.models import BookLevel, BookSide, Exchange, MarketBook, utcnow
from ..mapping.models import VenueMarket
from .base import (
    ExchangeClient,
    ExchangeError,
    GeoBlockedError,
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


def _json_list(value: Any) -> list:
    """Gamma encodes some array fields as JSON strings; decode either shape."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


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

    async def list_markets(
        self, *, page_size: int = 250, max_pages: int = 40, include_closed: bool = False
    ) -> list[VenueMarket]:
        """Discover markets via the Gamma metadata API (paginated by offset)."""
        gamma = self.config.gamma_base_url.rstrip("/")
        out: list[VenueMarket] = []
        offset = 0
        for _ in range(max_pages):
            params = {"limit": page_size, "offset": offset, "order": "id", "ascending": "true"}
            if not include_closed:
                params["closed"] = "false"
            data = await self._get(f"{gamma}/markets", params=params)
            batch = data if isinstance(data, list) else data.get("data", [])
            if not batch:
                break
            for raw in batch:
                vm = self._parse_gamma_market(raw)
                if vm is not None:
                    out.append(vm)
            if len(batch) < page_size:
                break
            offset += page_size
        return out

    @staticmethod
    def _parse_gamma_market(raw: dict) -> Optional[VenueMarket]:
        condition_id = raw.get("conditionId") or raw.get("condition_id")
        if not condition_id:
            return None
        outcomes = [str(o).strip().lower() for o in _json_list(raw.get("outcomes"))]
        tokens = [str(t) for t in _json_list(raw.get("clobTokenIds"))]
        yes_token = no_token = None
        for idx, outcome in enumerate(outcomes):
            if idx >= len(tokens):
                break
            if outcome in ("yes", "y"):
                yes_token = tokens[idx]
            elif outcome in ("no", "n"):
                no_token = tokens[idx]
        if yes_token is None and len(tokens) == 2:
            # Non-binary phrasing; assume [YES, NO] ordering as a fallback.
            yes_token, no_token = tokens[0], tokens[1]

        if raw.get("closed"):
            status = "closed"
        elif raw.get("active", True) is False:
            status = "inactive"
        else:
            status = "active"

        category = raw.get("category")
        if category is None:
            events = raw.get("events") or []
            if events and isinstance(events[0], dict):
                category = events[0].get("category")

        return VenueMarket(
            exchange=Exchange.POLYMARKET,
            venue_id=str(condition_id),
            title=raw.get("question", "") or raw.get("title", "") or "",
            description=raw.get("description", "") or "",
            category=category,
            close_time=_parse_iso(raw.get("endDate") or raw.get("end_date")),
            status=status,
            yes_token=yes_token,
            no_token=no_token,
            strike_type="binary",
            event_key=raw.get("slug"),
            raw=raw,
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
