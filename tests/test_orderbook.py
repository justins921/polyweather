"""Orderbook normalization: both Kalshi formats, best-first ordering."""

import asyncio

from clients.kalshi_svc import KalshiClient


def _client_with(response: dict) -> KalshiClient:
    client = KalshiClient.__new__(KalshiClient)  # bypass __init__ (no creds needed)

    async def fake_get(path, params=None):
        return response

    client._get = fake_get
    return client


def test_orderbook_fp_format_dollars_to_cents_best_first():
    # Real current-API shape: dollar strings, levels ascending (best LAST)
    client = _client_with({"orderbook_fp": {
        "yes_dollars": [["0.0100", "771.42"], ["0.0200", "200.30"]],
        "no_dollars": [["0.8800", "124.02"], ["0.9700", "5084.86"]],
    }})
    book = asyncio.run(KalshiClient.get_orderbook(client, "T"))
    assert book["yes"][0][0] == 2   # best yes bid first, converted to cents
    assert book["no"][0][0] == 97   # best no bid first
    # implied: bid=2, ask=100-97=3 → sane market


def test_orderbook_legacy_format_resorted_best_first():
    client = _client_with({"orderbook": {
        "yes": [[40, 10], [45, 5]],   # ascending in, best-first out
        "no": [[50, 10], [53, 4]],
    }})
    book = asyncio.run(KalshiClient.get_orderbook(client, "T"))
    assert book["yes"][0][0] == 45
    assert book["no"][0][0] == 53


def test_orderbook_empty_sides():
    client = _client_with({"orderbook_fp": {"yes_dollars": [], "no_dollars": None}})
    book = asyncio.run(KalshiClient.get_orderbook(client, "T"))
    assert book == {"yes": [], "no": []}


def _order_client(captured: dict) -> KalshiClient:
    client = KalshiClient.__new__(KalshiClient)

    async def fake_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {"order_id": "abc", "fill_count": "0.00", "remaining_count": "3.00"}

    client._post = fake_post
    return client


def test_place_order_v2_buy_yes_is_bid():
    from clients.kalshi_svc import OrderRequest

    captured = {}
    client = _order_client(captured)
    r = asyncio.run(KalshiClient.place_order(client, OrderRequest(
        ticker="KXHIGHNY-26AUG04-T81", action="buy", side="yes",
        count=3, yes_price=6)))
    assert captured["path"] == "/portfolio/events/orders"
    d = captured["data"]
    assert d["side"] == "bid"
    assert d["price"] == "0.0600"
    assert d["count"] == "3.00"
    assert "reduce_only" not in d
    assert r["order_id"] == "abc"


def test_place_order_v2_buy_no_is_ask():
    from clients.kalshi_svc import OrderRequest

    captured = {}
    client = _order_client(captured)
    # Buying NO at 41¢ — strategies encode this as yes_price = 59
    asyncio.run(KalshiClient.place_order(client, OrderRequest(
        ticker="T", action="buy", side="no", count=2, yes_price=59)))
    d = captured["data"]
    assert d["side"] == "ask"
    assert d["price"] == "0.5900"


def test_place_order_v2_sell_is_reduce_only():
    from clients.kalshi_svc import OrderRequest

    captured = {}
    client = _order_client(captured)
    asyncio.run(KalshiClient.place_order(client, OrderRequest(
        ticker="T", action="sell", side="yes", count=1, yes_price=50)))
    d = captured["data"]
    assert d["side"] == "ask"
    assert d["reduce_only"] is True
