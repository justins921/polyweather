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
