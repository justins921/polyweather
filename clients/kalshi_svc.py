"""
Async Kalshi REST + WebSocket client with RSA-PSS auth, token-bucket
rate limiter, and auto-reconnect.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import websockets
import websockets.exceptions

from settings import Settings

logger = logging.getLogger(__name__)


# ── Token-bucket rate limiter ────────────────────────────────────────────────


class TokenBucket:
    """
    Async token-bucket rate limiter.

    Allows `rate` operations per second, with a burst capacity equal to `rate`.
    """

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.tokens = float(rate)
        self.max_tokens = float(rate)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1


# ── RSA-PSS request signing ─────────────────────────────────────────────────


def _load_private_key(path: str):
    """Load an RSA private key from a PEM file."""
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, method: str, path: str, timestamp_ms: str) -> str:
    """RSA-PSS signature for Kalshi auth."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    msg = f"{timestamp_ms}{method}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


# ── Order representations ───────────────────────────────────────────────────


@dataclass
class OrderRequest:
    ticker: str
    action: str        # "buy" | "sell"
    side: str          # "yes" | "no"
    count: int         # integer contracts
    type: str = "limit"
    yes_price: int = 50  # cents 1-99
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "count": self.count,
            "type": self.type,
            "yes_price": self.yes_price,
            "client_order_id": self.client_order_id,
        }


# ── Kalshi client ───────────────────────────────────────────────────────────


class KalshiClient:
    """
    Async Kalshi REST + WS client.

    - REST via httpx.AsyncClient with per-request RSA-PSS signing.
    - WS via websockets with auto-reconnect and exponential backoff.
    - Token-bucket rate limiter for reads and writes independently.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._private_key = None
        self._http: httpx.AsyncClient | None = None
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None
        self._ws_callbacks: dict[str, list[Callable]] = {}
        self._read_limiter = TokenBucket(settings.read_rate_limit)
        self._write_limiter = TokenBucket(settings.write_rate_limit)
        self._ws_disconnect_count = 0
        self._ws_last_disconnect = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._settings.kalshi_private_key_path:
            self._private_key = _load_private_key(self._settings.kalshi_private_key_path)
        self._http = httpx.AsyncClient(timeout=30.0)
        logger.info("Kalshi REST client connected to %s", self._settings.rest_url)

    async def close(self) -> None:
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._http:
            await self._http.aclose()
        logger.info("Kalshi client closed")

    # ── Auth headers ─────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key:
            return {}
        ts = str(int(time.time() * 1000))
        path_no_qs = path.split("?")[0]
        sig = _sign(self._private_key, method, path_no_qs, ts)
        return {
            "KALSHI-ACCESS-KEY": self._settings.kalshi_api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    # ── REST helpers ─────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._read_limiter.acquire()
        url = self._settings.rest_url + path
        headers = self._auth_headers("GET", self._settings.kalshi_api_path + path)
        resp = await self._http.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        await self._write_limiter.acquire()
        url = self._settings.rest_url + path
        headers = self._auth_headers("POST", self._settings.kalshi_api_path + path)
        resp = await self._http.post(url, headers=headers, json=data)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict:
        await self._write_limiter.acquire()
        url = self._settings.rest_url + path
        headers = self._auth_headers("DELETE", self._settings.kalshi_api_path + path)
        resp = await self._http.delete(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── Market data ──────────────────────────────────────────────────────

    async def get_active_markets(
        self,
        status: str = "open",
        limit: int = 200,
        series_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch open markets (handles pagination).

        If *series_ticker* is given the server filters to that series,
        drastically reducing the number of pages returned.
        """
        all_markets: list[dict] = []
        cursor = ""
        pages = 0
        while True:
            params: dict[str, Any] = {"status": status, "limit": limit}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params)
            markets = data.get("markets", [])
            all_markets.extend(markets)
            cursor = data.get("cursor", "")
            pages += 1
            if not cursor or not markets:
                break
        logger.debug("Fetched %d markets in %d pages%s",
                     len(all_markets), pages,
                     f" (series={series_ticker})" if series_ticker else "")
        return all_markets

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self._get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """Fetch and normalize an orderbook.

        Returns {"yes": [[price_cents, qty], ...], "no": [...]} with levels
        sorted BEST-FIRST (highest bid at index 0). Handles both Kalshi
        formats: legacy "orderbook" (cents ints) and current "orderbook_fp"
        (dollar strings under yes_dollars/no_dollars). The raw API lists
        levels ascending, so the best bid is the LAST element — consumers
        here index [0], hence the re-sort.
        """
        data = await self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        ob = data.get("orderbook") or {}
        yes = ob.get("yes") or []
        no = ob.get("no") or []
        if not yes and not no:
            fp = data.get("orderbook_fp") or {}
            yes = [[round(float(p) * 100), float(q)]
                   for p, q in (fp.get("yes_dollars") or [])]
            no = [[round(float(p) * 100), float(q)]
                  for p, q in (fp.get("no_dollars") or [])]
        yes = sorted(([int(l[0]), l[1]] for l in yes), key=lambda l: -l[0])
        no = sorted(([int(l[0]), l[1]] for l in no), key=lambda l: -l[0])
        return {"yes": yes, "no": no}

    async def get_event(self, event_ticker: str) -> dict[str, Any]:
        return await self._get(f"/events/{event_ticker}")

    # ── Portfolio ────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return balance in dollars (Kalshi returns cents)."""
        data = await self._get("/portfolio/balance")
        return float(data.get("balance", 0)) / 100.0

    async def get_positions(self) -> list[dict[str, Any]]:
        data = await self._get("/portfolio/positions")
        return data.get("market_positions", [])

    async def get_open_orders(self) -> list[dict[str, Any]]:
        data = await self._get("/portfolio/orders", {"status": "resting"})
        return data.get("orders", [])

    # ── Order management ─────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Place a limit order. Returns the order dict from Kalshi."""
        logger.info(
            "Placing order: %s %s %s x%d @ %d¢ [%s]",
            order.action, order.side, order.ticker,
            order.count, order.yes_price, order.client_order_id[:8],
        )
        data = await self._post("/portfolio/orders", order.to_dict())
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._delete(f"/portfolio/orders/{order_id}")

    async def batch_cancel(self, order_ids: list[str]) -> list[dict]:
        """Cancel multiple orders. Returns list of results."""
        results = []
        for oid in order_ids:
            try:
                r = await self.cancel_order(oid)
                results.append({"order_id": oid, "success": True, **r})
            except Exception as exc:
                results.append({"order_id": oid, "success": False, "error": str(exc)})
        return results

    # ── WebSocket ────────────────────────────────────────────────────────

    async def start_ws(
        self,
        channels: list[str] | None = None,
        tickers: list[str] | None = None,
    ) -> None:
        """Start WebSocket connection with auto-reconnect."""
        self._ws_task = asyncio.create_task(
            self._ws_loop(channels or ["orderbook_delta"], tickers or [])
        )

    def on_ws_message(self, msg_type: str, callback: Callable) -> None:
        self._ws_callbacks.setdefault(msg_type, []).append(callback)

    async def _ws_loop(
        self, channels: list[str], tickers: list[str]
    ) -> None:
        backoff = 1
        while True:
            try:
                headers = self._auth_headers("GET", self._settings.kalshi_ws_path)
                async with websockets.connect(
                    self._settings.ws_url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("WebSocket connected")

                    # Subscribe
                    if channels and tickers:
                        sub_msg = {
                            "id": 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": channels,
                                "market_tickers": tickers,
                            },
                        }
                        await ws.send(json.dumps(sub_msg))

                    async for raw in ws:
                        msg = json.loads(raw)
                        msg_type = msg.get("type", "")
                        for cb in self._ws_callbacks.get(msg_type, []):
                            try:
                                await cb(msg) if asyncio.iscoroutinefunction(cb) else cb(msg)
                            except Exception:
                                logger.exception("WS callback error for %s", msg_type)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket disconnected, reconnecting in %ds", backoff)
            except Exception as exc:
                logger.error("WebSocket error: %s, reconnecting in %ds", exc, backoff)

            self._ws_disconnect_count += 1
            self._ws_last_disconnect = time.monotonic()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @property
    def ws_disconnect_count(self) -> int:
        return self._ws_disconnect_count

    @property
    def ws_last_disconnect(self) -> float:
        return self._ws_last_disconnect
