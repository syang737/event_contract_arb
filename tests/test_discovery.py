import httpx

from arb_engine.core.models import Exchange
from arb_engine.exchanges.kalshi_client import KalshiClient
from arb_engine.exchanges.polymarket_client import PolymarketClient
from arb_engine.mapping.models import VenueMarket
from arb_engine.storage.db import Database


# --------------------------------------------------------------------------- #
# Polymarket (Gamma) discovery
# --------------------------------------------------------------------------- #
def _gamma_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path != "/markets":
        return httpx.Response(404, json={})
    offset = int(request.url.params.get("offset", "0"))
    page1 = [
        {
            "conditionId": "0xAAA",
            "question": "Will a Democrat win the 2028 US presidential election?",
            "description": "Resolves YES if a Democrat wins.",
            "endDate": "2028-11-07T05:00:00Z",
            "closed": False,
            "active": True,
            "outcomes": "[\"Yes\", \"No\"]",
            "clobTokenIds": "[\"tokA_yes\", \"tokA_no\"]",
            "category": "politics",
        },
        {
            "conditionId": "0xBBB",
            "question": "Will the Fed cut in Jan 2026?",
            "outcomes": "[\"Yes\", \"No\"]",
            "clobTokenIds": "[\"tokB_yes\", \"tokB_no\"]",
            "closed": False,
        },
    ]
    page2 = [
        {
            "conditionId": "0xCCC",
            "question": "Will it rain?",
            "outcomes": "[\"Yes\", \"No\"]",
            "clobTokenIds": "[\"tokC_yes\", \"tokC_no\"]",
            "closed": False,
        }
    ]
    return httpx.Response(200, json=page1 if offset == 0 else page2 if offset == 2 else [])


async def test_polymarket_list_markets_parses_and_paginates():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_gamma_handler), base_url="https://clob")
    pm = PolymarketClient(client=client)
    markets = await pm.list_markets(page_size=2, max_pages=5)
    await client.aclose()

    assert [m.venue_id for m in markets] == ["0xAAA", "0xBBB", "0xCCC"]
    dem = markets[0]
    assert dem.exchange is Exchange.POLYMARKET
    assert dem.yes_token == "tokA_yes"
    assert dem.no_token == "tokA_no"
    assert dem.category == "politics"
    assert dem.close_time is not None and dem.close_time.year == 2028
    assert dem.is_open


# --------------------------------------------------------------------------- #
# Kalshi discovery
# --------------------------------------------------------------------------- #
def _kalshi_handler(request: httpx.Request) -> httpx.Response:
    if not request.url.path.endswith("/markets"):
        return httpx.Response(404, json={})
    cursor = request.url.params.get("cursor")
    if not cursor:
        return httpx.Response(200, json={"markets": [
            {"ticker": "PRES-2028-DEM", "event_ticker": "PRES-2028", "series_ticker": "PRES",
             "title": "2028 US presidential election", "yes_sub_title": "Democratic winner",
             "close_time": "2028-11-07T05:00:00Z", "status": "active", "strike_type": "binary",
             "category": "Politics"},
            {"ticker": "SPX-26DEC31-B5000", "title": "S&P 500 end of 2026",
             "yes_sub_title": "above 5000", "close_time": "2026-12-31T21:00:00Z",
             "status": "active", "strike_type": "greater", "floor_strike": 5000.0},
        ], "cursor": "c1"})
    return httpx.Response(200, json={"markets": [
        {"ticker": "DECOY-KA", "title": "Weather", "status": "active"},
    ], "cursor": ""})


async def test_kalshi_list_markets_parses_and_paginates():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_kalshi_handler), base_url="https://kalshi")
    ka = KalshiClient(client=client)
    markets = await ka.list_markets(page_size=2, max_pages=5)
    await client.aclose()

    assert [m.venue_id for m in markets] == ["PRES-2028-DEM", "SPX-26DEC31-B5000", "DECOY-KA"]
    spx = markets[1]
    assert spx.strike_type == "greater"
    assert spx.strike == 5000.0
    assert "above 5000" in spx.title  # yes_sub_title folded into the title
    pres = markets[0]
    assert pres.event_key == "PRES-2028"
    assert pres.category == "Politics"


# --------------------------------------------------------------------------- #
# Cache upsert
# --------------------------------------------------------------------------- #
def test_upsert_venue_markets_is_idempotent():
    db = Database("sqlite:///:memory:")
    catalog = [
        VenueMarket(Exchange.POLYMARKET, "0xAAA", "Dem 2028", status="active"),
        VenueMarket(Exchange.KALSHI, "PRES-2028-DEM", "Dem 2028", status="active"),
    ]
    db.upsert_venue_markets(catalog)
    db.upsert_venue_markets(catalog)  # second upsert must not duplicate
    rows = db.venue_markets()
    assert len(rows) == 2

    # Update in place (status change) keeps the same row.
    catalog[0].status = "closed"
    db.upsert_venue_markets(catalog)
    pm_rows = db.venue_markets(Exchange.POLYMARKET)
    assert len(pm_rows) == 1
    assert pm_rows[0].status == "closed"
