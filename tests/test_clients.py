import httpx
import pytest

from arb_engine.exchanges.base import GeoBlockedError
from arb_engine.exchanges.kalshi_client import KalshiClient
from arb_engine.exchanges.polymarket_client import PolymarketClient

from .conftest import make_mapping


# --------------------------------------------------------------------------- #
# Polymarket
# --------------------------------------------------------------------------- #
def _pm_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/book":
        token = request.url.params.get("token_id")
        if token == "m1_Y":
            return httpx.Response(200, json={"bids": [{"price": "0.38", "size": "50"}],
                                             "asks": [{"price": "0.40", "size": "100"}]})
        return httpx.Response(200, json={"bids": [{"price": "0.53", "size": "60"}],
                                         "asks": [{"price": "0.55", "size": "120"}]})
    if path.startswith("/markets/"):
        return httpx.Response(200, json={
            "closed": True,
            "tokens": [
                {"token_id": "m1_Y", "outcome": "Yes", "winner": True},
                {"token_id": "m1_N", "outcome": "No", "winner": False},
            ],
        })
    return httpx.Response(404, json={})


async def test_polymarket_book_parsing():
    mapping = make_mapping()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_pm_handler), base_url="https://clob")
    pm = PolymarketClient(client=client)
    book = await pm.fetch_book(mapping)
    assert book.yes.best_ask == 0.40
    assert book.yes.asks[0].size == 100
    assert book.no.best_ask == 0.55
    await client.aclose()


async def test_polymarket_resolution():
    mapping = make_mapping()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_pm_handler), base_url="https://clob")
    pm = PolymarketClient(client=client)
    res = await pm.get_resolution(mapping)
    assert res.resolved and res.yes_won is True
    assert res.settlement_price_yes == 1.0
    await client.aclose()


async def test_polymarket_geoblock_is_hard_error():
    def handler(request):
        return httpx.Response(403, text="blocked")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://clob")
    pm = PolymarketClient(client=client)
    with pytest.raises(GeoBlockedError):
        await pm.fetch_book(make_mapping())
    await client.aclose()


# --------------------------------------------------------------------------- #
# Kalshi
# --------------------------------------------------------------------------- #
def _ka_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/orderbook"):
        return httpx.Response(200, json={"orderbook": {
            "yes": [[40, 100], [39, 50]],   # YES bids
            "no": [[55, 120], [54, 60]],    # NO bids
        }})
    if "/markets/" in path:
        return httpx.Response(200, json={"market": {"status": "finalized", "result": "yes"}})
    return httpx.Response(404, json={})


async def test_kalshi_book_normalization():
    mapping = make_mapping()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ka_handler), base_url="https://kalshi")
    ka = KalshiClient(client=client)
    book = await ka.fetch_book(mapping)

    # YES bids straight from `yes`; best YES ask derived from best NO bid (55c).
    assert book.yes.best_bid == 0.40
    assert book.yes.best_ask == (100 - 55) / 100  # 0.45
    # NO bids straight from `no`; best NO ask derived from best YES bid (40c).
    assert book.no.best_bid == 0.55
    assert book.no.best_ask == (100 - 40) / 100  # 0.60
    await client.aclose()


async def test_kalshi_resolution():
    mapping = make_mapping()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ka_handler), base_url="https://kalshi")
    ka = KalshiClient(client=client)
    res = await ka.get_resolution(mapping)
    assert res.resolved and res.yes_won is True
    await client.aclose()
