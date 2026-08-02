"""
Risk manager — enforces every non-negotiable limit.

Tracks:
- daily P&L / loss
- total open exposure (notional)
- per-market exposure
- concurrent market count
- API error rate (kill switch)
- WS disconnect rate (kill switch)
- per-market circuit breakers (adverse selection)
- category allowlist
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class MarketExposure:
    """Track notional exposure per market."""

    ticker: str
    net_contracts: int = 0       # +long, -short
    avg_price_cents: int = 0
    notional: float = 0.0        # abs $ exposure
    last_fill_ts: float = 0.0


@dataclass
class CircuitBreaker:
    """Per-market adverse-selection cooldown."""

    ticker: str
    triggered_at: float = 0.0
    cooldown_until: float = 0.0
    reason: str = ""


class RiskManager:
    """Centralised risk enforcement — call before every order."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

        # Daily loss tracking
        self._daily_loss: float = 0.0
        self._daily_date: date = datetime.now(timezone.utc).date()

        # Exposure per market
        self._exposures: dict[str, MarketExposure] = {}

        # Circuit breakers
        self._breakers: dict[str, CircuitBreaker] = {}

        # API error tracking (rolling window)
        self._api_calls: deque[tuple[float, bool]] = deque()  # (ts, is_error)

        # Kill switch
        self._killed = False
        self._kill_reason = ""
        self._kill_ts = 0.0

        # Category disablement
        self._disabled_categories: set[str] = set()

        # Fill history for adverse-selection detection
        self._recent_fills: dict[str, deque] = {}  # ticker -> deque of (ts, side, mid_at_fill)

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def daily_loss(self) -> float:
        self._maybe_reset_daily()
        return self._daily_loss

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    # ── Daily reset ──────────────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_date:
            logger.info("New trading day — resetting daily loss counter")
            self._daily_loss = 0.0
            self._daily_date = today

    def maybe_reset_daily(self) -> None:
        self._maybe_reset_daily()

    # ── Daily loss ───────────────────────────────────────────────────────

    def daily_loss_exceeded(self) -> bool:
        self._maybe_reset_daily()
        return self._daily_loss >= self._s.max_daily_loss

    def record_pnl(self, pnl: float) -> None:
        """Record realized P&L — tracks net daily loss (wins offset losses)."""
        self._maybe_reset_daily()
        self._daily_loss = max(0.0, self._daily_loss - pnl)
        logger.info("Recorded P&L: $%.4f  daily_loss=$%.4f", pnl, self._daily_loss,
                     extra={"daily_loss": self._daily_loss, "pnl": pnl})

    # ── Exposure tracking ────────────────────────────────────────────────

    def total_exposure(self) -> float:
        return sum(e.notional for e in self._exposures.values())

    def market_exposure(self, ticker: str) -> float:
        e = self._exposures.get(ticker)
        return e.notional if e else 0.0

    def active_market_count(self) -> int:
        return sum(1 for e in self._exposures.values() if e.notional > 0)

    def record_fill(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        mid_cents: int | None = None,
    ) -> None:
        """Update exposure after a fill."""
        exp = self._exposures.setdefault(ticker, MarketExposure(ticker=ticker))

        delta = count if side == "yes" else -count
        exp.net_contracts += delta
        exp.notional = abs(exp.net_contracts) * price_cents / 100.0
        exp.avg_price_cents = price_cents
        exp.last_fill_ts = time.monotonic()

        # Track for adverse-selection
        if mid_cents is not None:
            fills = self._recent_fills.setdefault(ticker, deque(maxlen=50))
            fills.append((time.monotonic(), side, mid_cents))

        logger.info(
            "Fill recorded: %s %s x%d @ %d¢  net=%d  notional=$%.2f",
            ticker, side, count, price_cents, exp.net_contracts, exp.notional,
            extra={"ticker": ticker, "side": side, "size": count, "exposure": exp.notional},
        )

    def close_position(self, ticker: str) -> None:
        if ticker in self._exposures:
            self._exposures[ticker].net_contracts = 0
            self._exposures[ticker].notional = 0.0

    # ── Pre-order checks ─────────────────────────────────────────────────

    def can_place_order(
        self,
        ticker: str,
        notional: float,
        category: str = "",
    ) -> tuple[bool, str]:
        """
        Check ALL risk limits before placing an order.

        Returns (allowed, reason).
        """
        self._maybe_reset_daily()

        # Kill switch
        if self._killed:
            return False, f"Kill switch active: {self._kill_reason}"

        # Daily loss
        if self._daily_loss >= self._s.max_daily_loss:
            return False, f"Daily loss ${self._daily_loss:.2f} >= limit ${self._s.max_daily_loss:.2f}"

        # Category check
        if category:
            cat_lower = category.lower()
            if cat_lower in self._disabled_categories:
                return False, f"Category '{cat_lower}' is disabled"

            sports_cats = {"sports", "esports", "football", "basketball",
                           "baseball", "hockey", "soccer", "tennis",
                           "golf", "mma", "boxing"}
            is_sport = cat_lower in sports_cats
            if is_sport and not self._s.allow_sports:
                return False, "Sports category disallowed by config"
            if not is_sport and not self._s.allow_non_sports:
                return False, "Non-sports category disallowed by config"

        # Per-order notional
        if notional > self._s.max_per_order_notional:
            return False, f"Order notional ${notional:.2f} > limit ${self._s.max_per_order_notional:.2f}"

        # Per-market notional
        current = self.market_exposure(ticker)
        if current + notional > self._s.max_per_market_notional:
            return False, (
                f"Market exposure ${current + notional:.2f} would exceed "
                f"limit ${self._s.max_per_market_notional:.2f}"
            )

        # Total exposure
        total = self.total_exposure()
        if total + notional > self._s.max_total_exposure:
            return False, (
                f"Total exposure ${total + notional:.2f} would exceed "
                f"limit ${self._s.max_total_exposure:.2f}"
            )

        # Concurrent markets
        new_market = ticker not in self._exposures or self._exposures[ticker].notional == 0
        if new_market and self.active_market_count() >= self._s.max_concurrent_markets:
            return False, (
                f"Concurrent markets {self.active_market_count()} >= "
                f"limit {self._s.max_concurrent_markets}"
            )

        # Circuit breaker
        breaker = self._breakers.get(ticker)
        if breaker and time.monotonic() < breaker.cooldown_until:
            remaining = breaker.cooldown_until - time.monotonic()
            return False, f"Circuit breaker active for {ticker} ({remaining:.0f}s remaining)"

        return True, "OK"

    # ── Circuit breaker / adverse selection ───────────────────────────────

    def check_adverse_selection(self, ticker: str, current_mid: int) -> bool:
        """
        Check if recent fills show adverse selection.

        If you got filled and mid moved against you by ≥ threshold ticks
        within the window, trigger circuit breaker.

        Returns True if adverse selection detected.
        """
        fills = self._recent_fills.get(ticker)
        if not fills:
            return False

        now = time.monotonic()
        window = self._s.circuit_breaker_window_secs
        threshold = self._s.circuit_breaker_adverse_ticks

        for ts, side, mid_at_fill in fills:
            if now - ts > window:
                continue
            if side == "yes":
                # Bought YES — adverse if mid dropped
                move = mid_at_fill - current_mid
            else:
                # Bought NO — adverse if mid rose
                move = current_mid - mid_at_fill
            if move >= threshold:
                self._trigger_breaker(
                    ticker,
                    f"Adverse move {move} ticks against {side} fill "
                    f"(mid {mid_at_fill}→{current_mid})",
                )
                return True
        return False

    def _trigger_breaker(self, ticker: str, reason: str) -> None:
        now = time.monotonic()
        self._breakers[ticker] = CircuitBreaker(
            ticker=ticker,
            triggered_at=now,
            cooldown_until=now + self._s.circuit_breaker_cooldown_secs,
            reason=reason,
        )
        logger.warning(
            "CIRCUIT BREAKER: %s — %s (cooldown %ds)",
            ticker, reason, self._s.circuit_breaker_cooldown_secs,
            extra={"ticker": ticker, "reason": reason, "action": "circuit_breaker"},
        )

    # ── Kill switch ──────────────────────────────────────────────────────

    def record_api_error(self) -> None:
        self._api_calls.append((time.monotonic(), True))
        self._check_kill_switch()

    def record_api_success(self) -> None:
        self._api_calls.append((time.monotonic(), False))

    def _check_kill_switch(self) -> None:
        now = time.monotonic()
        window = self._s.kill_switch_window_secs
        # Prune old entries
        while self._api_calls and now - self._api_calls[0][0] > window:
            self._api_calls.popleft()
        if len(self._api_calls) < 10:
            return  # not enough data
        errors = sum(1 for _, is_err in self._api_calls if is_err)
        rate = (errors / len(self._api_calls)) * 100
        if rate > self._s.kill_switch_error_pct:
            self._killed = True
            self._kill_reason = f"API error rate {rate:.1f}% > {self._s.kill_switch_error_pct}%"
            self._kill_ts = now
            logger.critical(
                "KILL SWITCH: %s", self._kill_reason,
                extra={"action": "kill_switch", "reason": self._kill_reason},
            )

    def check_ws_kill(self, disconnect_count: int, last_disconnect: float) -> None:
        """Check WS disconnect rate and trigger kill switch if needed."""
        if disconnect_count >= self._s.kill_switch_ws_max_disconnects:
            now = time.monotonic()
            if now - last_disconnect < self._s.kill_switch_ws_window_secs:
                self._killed = True
                self._kill_reason = (
                    f"WS disconnected {disconnect_count} times in "
                    f"{self._s.kill_switch_ws_window_secs}s"
                )
                logger.critical("KILL SWITCH: %s", self._kill_reason)

    def is_killed(self) -> bool:
        return self._killed

    def maybe_reset_kill_switch(self) -> None:
        """Allow recovery after cooldown period."""
        if self._killed and time.monotonic() - self._kill_ts > self._s.kill_switch_window_secs:
            logger.info("Kill switch cooldown expired — resetting")
            self._killed = False
            self._kill_reason = ""
            self._api_calls.clear()

    # ── Category management ──────────────────────────────────────────────

    def disable_category(self, category: str) -> None:
        self._disabled_categories.add(category.lower())
        logger.info("Category disabled: %s", category)

    def enable_category(self, category: str) -> None:
        self._disabled_categories.discard(category.lower())

    def is_category_enabled(self, category: str) -> bool:
        return category.lower() not in self._disabled_categories

    # ── Summary ──────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        return {
            "daily_loss": round(self._daily_loss, 4),
            "total_exposure": round(self.total_exposure(), 4),
            "active_markets": self.active_market_count(),
            "killed": self._killed,
            "kill_reason": self._kill_reason,
            "disabled_categories": list(self._disabled_categories),
            "circuit_breakers": [
                {"ticker": b.ticker, "reason": b.reason}
                for b in self._breakers.values()
                if time.monotonic() < b.cooldown_until
            ],
        }
