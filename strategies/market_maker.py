"""
Strategy A: Inventory-skewed micro market maker.

Places passive limit orders near BBO, applying inventory skew to
manage net exposure.  Only quotes when net_edge > 0 after fees.

Key behaviors:
- Quotes both sides (bid + ask) around mid
- Skews prices based on current inventory (long → widen bids, tighten asks)
- Refreshes quotes every N seconds, but only if price moved enough
- Detects adverse selection and triggers circuit breaker
- Pulls quotes during high-volatility windows
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from clients.kalshi_svc import KalshiClient, OrderRequest
from data.storage import Storage
from engine.fee_calculator import FeeModel
from engine.risk_manager import RiskManager
from settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class QuoteState:
    """Tracks resting orders for a single market."""

    ticker: str
    bid_order_id: str | None = None
    ask_order_id: str | None = None
    bid_price: int = 0       # cents
    ask_price: int = 0       # cents
    bid_size: int = 0
    ask_size: int = 0
    last_refresh: float = 0.0
    last_mid: int = 0        # last known midpoint


class MarketMakerStrategy:
    """
    Inventory-skewed micro market maker.

    One instance manages quotes across all eligible markets.
    """

    def __init__(
        self,
        settings: Settings,
        client: KalshiClient,
        fee_model: FeeModel,
        risk_mgr: RiskManager,
        storage: Storage,
    ) -> None:
        self._s = settings
        self._client = client
        self._fee = fee_model
        self._risk = risk_mgr
        self._storage = storage
        self._quotes: dict[str, QuoteState] = {}

    # ── Public interface ─────────────────────────────────────────────────

    async def process_market(self, market: dict[str, Any]) -> None:
        """
        Evaluate a single market and place/update quotes if profitable.

        Called once per cycle per eligible market.
        """
        ticker = market.get("ticker", "")
        if not ticker:
            return

        # Fetch current orderbook
        try:
            book = await self._client.get_orderbook(ticker)
        except Exception as exc:
            logger.warning("Failed to fetch book for %s: %s", ticker, exc)
            self._risk.record_api_error()
            return
        self._risk.record_api_success()

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        if not yes_bids or not no_bids:
            return

        # Kalshi book: yes bids = [[price_cents, size], ...]
        best_bid = int(yes_bids[0][0]) if yes_bids else 0
        best_ask = 100 - int(no_bids[0][0]) if no_bids else 100
        # Alternate: some APIs return yes_ask directly
        if best_bid <= 0 or best_ask >= 100 or best_bid >= best_ask:
            return

        bid_depth = sum(lvl[1] for lvl in yes_bids[:3]) if yes_bids else 0
        ask_depth = sum(lvl[1] for lvl in no_bids[:3]) if no_bids else 0

        # Depth check
        if bid_depth < self._s.min_book_depth and ask_depth < self._s.min_book_depth:
            logger.info("MM skip %s: thin book (bid_depth=%d, ask_depth=%d)",
                        ticker, bid_depth, ask_depth)
            return

        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
        net_edge = self._fee.net_edge_cents(best_bid, best_ask, is_maker=True)

        # ── Adverse selection check ──────────────────────────────────────
        if self._risk.check_adverse_selection(ticker, int(mid)):
            logger.info("MM skip %s: adverse selection detected — pulling quotes", ticker)
            await self._pull_quotes(ticker)
            return

        # ── Net edge gate ────────────────────────────────────────────────
        if not self._fee.passes_gate(best_bid, best_ask, is_maker=True):
            logger.info(
                "MM skip %s: negative edge (bid=%d ask=%d spread=%d edge=%.2f¢)",
                ticker, best_bid, best_ask, spread, net_edge,
            )
            await self._pull_quotes(ticker)
            return

        # ── Compute skewed quotes ────────────────────────────────────────
        inventory = self._risk.market_exposure(ticker)
        net_contracts = 0
        exp = self._risk._exposures.get(ticker)
        if exp:
            net_contracts = exp.net_contracts

        bid_price, ask_price = self._compute_skewed_prices(
            best_bid, best_ask, net_contracts,
        )

        # Size: always 1-2 contracts to start
        count = self._compute_size(ticker, bid_price, market)

        if count <= 0:
            logger.info("MM skip %s: size=0 (exposure limit reached)", ticker)
            await self._pull_quotes(ticker)
            return

        # ── Check if refresh needed ──────────────────────────────────────
        state = self._quotes.get(ticker)
        now = time.monotonic()

        if state and not self._needs_refresh(state, bid_price, ask_price, now):
            return

        logger.info(
            "MM quote %s: bid=%d ask=%d size=%d mid=%.0f spread=%d edge=%.2f¢ inv=%d",
            ticker, bid_price, ask_price, count, mid, spread, net_edge, net_contracts,
        )

        # ── Place/update quotes ──────────────────────────────────────────
        await self._update_quotes(
            ticker=ticker,
            bid_price=bid_price,
            ask_price=ask_price,
            count=count,
            mid=int(mid),
            category=market.get("category", ""),
        )

    async def cancel_all(self) -> None:
        """Cancel all resting orders (shutdown)."""
        for ticker, state in self._quotes.items():
            await self._pull_quotes(ticker)
        self._quotes.clear()

    # ── Quote computation ────────────────────────────────────────────────

    def _compute_skewed_prices(
        self,
        best_bid: int,
        best_ask: int,
        net_contracts: int,
    ) -> tuple[int, int]:
        """
        Compute bid and ask prices with inventory skew.

        If long (net_contracts > 0): widen bid (lower), tighten ask (lower)
        If short (net_contracts < 0): tighten bid (higher), widen ask (higher)
        """
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0
        half = max(spread / 2.0, 1)

        # Skew: shift mid by skew_per_contract * net_contracts
        skew = self._s.mm_skew_per_contract * net_contracts
        skewed_mid = mid - skew  # negative skew = we want to sell → lower mid

        # Our bid and ask: inside the current spread by 1 tick
        our_bid = int(skewed_mid - half + 1)
        our_ask = int(skewed_mid + half - 1)

        # Clamp to valid range and ensure we don't cross
        our_bid = max(1, min(our_bid, 98))
        our_ask = max(2, min(our_ask, 99))
        if our_bid >= our_ask:
            our_bid = our_ask - 1

        return our_bid, our_ask

    def _compute_size(
        self,
        ticker: str,
        price_cents: int,
        market: dict[str, Any],
    ) -> int:
        """Compute order size respecting all risk limits."""
        max_notional = min(
            self._s.max_per_order_notional,
            self._s.max_per_market_notional - self._risk.market_exposure(ticker),
            self._s.max_total_exposure - self._risk.total_exposure(),
        )
        if max_notional <= 0:
            return 0

        count = FeeModel.max_contracts(max_notional, price_cents)
        # Cap at small size for micro-bankroll
        count = min(count, 2)
        # Final risk gate
        notional = count * price_cents / 100.0
        ok, reason = self._risk.can_place_order(
            ticker, notional, category=market.get("category", ""),
        )
        if not ok:
            logger.debug("Size rejected for %s: %s", ticker, reason)
            return 0
        return count

    def _needs_refresh(
        self,
        state: QuoteState,
        new_bid: int,
        new_ask: int,
        now: float,
    ) -> bool:
        """Only refresh if price moved enough or enough time elapsed."""
        elapsed = now - state.last_refresh
        if elapsed < self._s.mm_quote_refresh_secs:
            bid_moved = abs(new_bid - state.bid_price) >= self._s.mm_min_price_move_ticks
            ask_moved = abs(new_ask - state.ask_price) >= self._s.mm_min_price_move_ticks
            if not bid_moved and not ask_moved:
                return False
        return True

    # ── Order management ─────────────────────────────────────────────────

    async def _update_quotes(
        self,
        ticker: str,
        bid_price: int,
        ask_price: int,
        count: int,
        mid: int,
        category: str = "",
    ) -> None:
        """Cancel old quotes and place new ones."""
        state = self._quotes.get(ticker, QuoteState(ticker=ticker))

        # Cancel existing orders
        await self._pull_quotes(ticker)

        # Place bid (buy YES at bid_price)
        bid_notional = count * bid_price / 100.0
        ok, reason = self._risk.can_place_order(ticker, bid_notional, category)
        if ok:
            try:
                bid_order = OrderRequest(
                    ticker=ticker,
                    action="buy",
                    side="yes",
                    count=count,
                    yes_price=bid_price,
                )
                result = await self._client.place_order(bid_order)
                state.bid_order_id = result.get("order_id")
                state.bid_price = bid_price
                state.bid_size = count
            except Exception as exc:
                logger.warning("Bid placement failed for %s: %s", ticker, exc)
                self._risk.record_api_error()
        else:
            logger.debug("Bid blocked for %s: %s", ticker, reason)

        # Place ask (buy NO at 100-ask_price)
        no_price = 100 - ask_price
        ask_notional = count * no_price / 100.0
        ok, reason = self._risk.can_place_order(ticker, ask_notional, category)
        if ok:
            try:
                ask_order = OrderRequest(
                    ticker=ticker,
                    action="buy",
                    side="no",
                    count=count,
                    yes_price=ask_price,
                )
                result = await self._client.place_order(ask_order)
                state.ask_order_id = result.get("order_id")
                state.ask_price = ask_price
                state.ask_size = count
            except Exception as exc:
                logger.warning("Ask placement failed for %s: %s", ticker, exc)
                self._risk.record_api_error()
        else:
            logger.debug("Ask blocked for %s: %s", ticker, reason)

        state.last_refresh = time.monotonic()
        state.last_mid = mid
        self._quotes[ticker] = state

        # Log to storage
        await self._storage.log_quote(
            ticker=ticker,
            bid=bid_price,
            ask=ask_price,
            size=count,
            mid=mid,
            net_edge_cents=round(self._fee.net_edge_cents(
                bid_price, ask_price, is_maker=True
            ), 2),
        )

    async def _pull_quotes(self, ticker: str) -> None:
        """Cancel all resting orders for a market."""
        state = self._quotes.get(ticker)
        if not state:
            return

        ids_to_cancel = []
        if state.bid_order_id:
            ids_to_cancel.append(state.bid_order_id)
        if state.ask_order_id:
            ids_to_cancel.append(state.ask_order_id)

        if ids_to_cancel:
            try:
                await self._client.batch_cancel(ids_to_cancel)
            except Exception as exc:
                logger.warning("Cancel failed for %s: %s", ticker, exc)

        state.bid_order_id = None
        state.ask_order_id = None
