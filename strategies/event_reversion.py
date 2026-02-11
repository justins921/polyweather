"""
Strategy B: Event window mean reversion.

Monitors price deviations from a rolling mean and enters small
positions when price spikes beyond N standard deviations, expecting
reversion.

Only runs in selected markets with adequate liquidity.
Every trade must pass the net_edge gate.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from clients.kalshi_svc import KalshiClient, OrderRequest
from data.storage import Storage
from engine.fee_calculator import FeeModel
from engine.risk_manager import RiskManager
from settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class PriceWindow:
    """Rolling price window for a single market."""

    ticker: str
    prices: deque = field(default_factory=lambda: deque(maxlen=200))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=200))
    active_order_id: str | None = None
    active_side: str = ""
    entry_price: int = 0
    entry_time: float = 0.0


class EventReversionStrategy:
    """
    Mean-reversion strategy for event-driven price spikes.

    - Maintains a rolling window of mid prices per market
    - When zscore > threshold and spread is reasonable, enters small position
    - Exits at take-profit, stop-loss, or time-out
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
        self._windows: dict[str, PriceWindow] = {}

    async def process_market(self, market: dict[str, Any]) -> None:
        """Evaluate a market for mean-reversion entry or manage existing position."""
        ticker = market.get("ticker", "")
        if not ticker:
            return

        # Fetch orderbook for current prices
        try:
            book = await self._client.get_orderbook(ticker)
        except Exception as exc:
            logger.debug("ER: book fetch failed for %s: %s", ticker, exc)
            return

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])
        if not yes_bids or not no_bids:
            return

        best_bid = int(yes_bids[0][0])
        best_ask = 100 - int(no_bids[0][0])
        if best_bid >= best_ask or best_bid <= 0 or best_ask >= 100:
            return

        mid = (best_bid + best_ask) / 2.0

        # Update rolling window
        window = self._windows.setdefault(ticker, PriceWindow(ticker=ticker))
        window.prices.append(mid)
        window.timestamps.append(time.monotonic())

        # Need enough data
        if len(window.prices) < self._s.er_rolling_window:
            logger.debug("ER %s: building window (%d/%d samples)",
                         ticker, len(window.prices), self._s.er_rolling_window)
            return

        # Check existing position for exit
        if window.active_order_id:
            await self._check_exit(window, mid)
            return

        # ── Entry signal ─────────────────────────────────────────────────
        prices = np.array(list(window.prices))
        mean = prices[-self._s.er_rolling_window :].mean()
        std = prices[-self._s.er_rolling_window :].std()

        if std < 0.5:
            logger.debug("ER skip %s: no volatility (std=%.2f)", ticker, std)
            return

        zscore = (mid - mean) / std

        if abs(zscore) < self._s.er_entry_zscore:
            logger.debug("ER %s: no signal (zscore=%.2f, need ±%.1f, mid=%.0f, mean=%.1f)",
                         ticker, zscore, self._s.er_entry_zscore, mid, mean)
            return

        # Net edge gate
        if not self._fee.passes_gate(best_bid, best_ask, is_maker=True):
            logger.info("ER skip %s: signal (z=%.2f) but no edge (bid=%d ask=%d)",
                        ticker, zscore, best_bid, best_ask)
            return

        # Determine side: if price spiked UP (zscore > 0), bet on reversion DOWN → buy NO
        #                  if price spiked DOWN (zscore < 0), bet on reversion UP → buy YES
        if zscore > 0:
            side = "no"
            price_cents = 100 - best_ask  # buying NO at this price
        else:
            side = "yes"
            price_cents = best_bid + 1  # buying YES slightly above bid

        price_cents = max(1, min(99, price_cents))

        # Size: small, capped at er_max_notional
        count = FeeModel.max_contracts(self._s.er_max_notional, price_cents)
        count = max(1, min(count, 2))  # 1-2 contracts max

        notional = count * price_cents / 100.0
        ok, reason = self._risk.can_place_order(
            ticker, notional, category=market.get("category", ""),
        )
        if not ok:
            logger.debug("ER: order blocked for %s: %s", ticker, reason)
            return

        # Place entry order
        try:
            order = OrderRequest(
                ticker=ticker,
                action="buy",
                side=side,
                count=count,
                yes_price=price_cents if side == "yes" else (100 - price_cents),
            )
            result = await self._client.place_order(order)
            window.active_order_id = result.get("order_id")
            window.active_side = side
            window.entry_price = price_cents
            window.entry_time = time.monotonic()

            logger.info(
                "ER: Entry %s %s x%d @ %d¢ (zscore=%.2f)",
                side, ticker, count, price_cents, zscore,
                extra={"ticker": ticker, "side": side, "action": "er_entry"},
            )

            await self._storage.log_trade(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=price_cents,
                strategy="event_reversion",
                reason=f"zscore={zscore:.2f}",
            )

        except Exception as exc:
            logger.warning("ER: entry order failed for %s: %s", ticker, exc)
            self._risk.record_api_error()

    async def _check_exit(self, window: PriceWindow, current_mid: float) -> None:
        """Check if we should exit an active position."""
        entry = window.entry_price
        tp = self._s.er_take_profit_ticks
        sl = self._s.er_stop_loss_ticks

        if window.active_side == "yes":
            profit_ticks = current_mid - entry
        else:
            profit_ticks = entry - current_mid

        exit_reason = ""
        if profit_ticks >= tp:
            exit_reason = f"take_profit ({profit_ticks:.0f} ticks)"
        elif profit_ticks <= -sl:
            exit_reason = f"stop_loss ({profit_ticks:.0f} ticks)"

        if not exit_reason:
            return

        # Cancel resting order and flatten
        try:
            if window.active_order_id:
                await self._client.cancel_order(window.active_order_id)
        except Exception:
            pass

        logger.info(
            "ER: Exit %s %s @ mid=%.0f (entry=%d, %s)",
            window.active_side, window.ticker, current_mid, entry, exit_reason,
            extra={"ticker": window.ticker, "action": "er_exit", "reason": exit_reason},
        )

        # Record P&L
        pnl_cents = profit_ticks
        pnl_dollars = pnl_cents / 100.0
        self._risk.record_pnl(pnl_dollars)

        await self._storage.log_trade(
            ticker=window.ticker,
            side=window.active_side,
            count=1,
            price_cents=int(current_mid),
            strategy="event_reversion",
            reason=exit_reason,
            pnl=round(pnl_dollars, 4),
        )

        # Reset
        window.active_order_id = None
        window.active_side = ""
        window.entry_price = 0
        window.entry_time = 0.0
