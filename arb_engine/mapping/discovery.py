"""Catalog discovery: fetch both venues' markets and cache them."""

from __future__ import annotations

from ..core.models import Exchange
from ..exchanges.base import ExchangeClient
from ..storage.db import Database
from .models import VenueMarket


async def refresh_catalog(
    db: Database,
    pm_client: ExchangeClient,
    ka_client: ExchangeClient,
    **list_kwargs,
) -> dict[Exchange, list[VenueMarket]]:
    """Fetch both catalogs, upsert them into ``venue_markets``, and return them."""
    pm_markets = await pm_client.list_markets(**list_kwargs)
    ka_markets = await ka_client.list_markets(**list_kwargs)
    db.upsert_venue_markets(pm_markets + ka_markets)
    return {Exchange.POLYMARKET: pm_markets, Exchange.KALSHI: ka_markets}
