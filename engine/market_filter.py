"""
Market filter — selects contracts suitable for small-bankroll strategies.

Applies depth, spread, liquidity, category, and time-to-expiry checks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from engine.risk_manager import RiskManager
from settings import Settings

logger = logging.getLogger(__name__)


class MarketFilter:
    """Filter raw Kalshi markets down to those worth quoting."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def filter(
        self,
        markets: list[dict[str, Any]],
        risk_mgr: RiskManager,
    ) -> list[dict[str, Any]]:
        """Return only markets that pass all filters."""
        eligible = []
        rejection_reasons: dict[str, int] = {}
        for m in markets:
            ok, reason = self.check(m, risk_mgr)
            if ok:
                eligible.append(m)
            else:
                # Bucket by reason prefix for summary
                bucket = reason.split("=")[0].split("(")[0].strip()
                rejection_reasons[bucket] = rejection_reasons.get(bucket, 0) + 1
                logger.debug(
                    "Filtered out %s: %s", m.get("ticker", "?"), reason,
                )

        # Log filtering summary at INFO level
        if markets:
            top_reasons = sorted(rejection_reasons.items(), key=lambda x: -x[1])[:5]
            reasons_str = ", ".join(f"{r}: {c}" for r, c in top_reasons)
            logger.info(
                "Filter: %d/%d markets eligible  [rejected: %s]",
                len(eligible), len(markets), reasons_str or "none",
            )
            if eligible:
                tickers = [m.get("ticker", "?") for m in eligible[:10]]
                logger.info("Eligible markets: %s%s",
                            ", ".join(tickers),
                            f" (+{len(eligible)-10} more)" if len(eligible) > 10 else "")

        return eligible

    def check(
        self,
        market: dict[str, Any],
        risk_mgr: RiskManager,
    ) -> tuple[bool, str]:
        """Check a single market against all criteria."""
        ticker = market.get("ticker", "")
        status = market.get("status", "")

        if status not in ("open", "active"):
            return False, f"status={status}"

        # Category check
        category = market.get("category", "")
        if category and not risk_mgr.is_category_enabled(category):
            return False, f"category '{category}' disabled"

        cat_lower = category.lower()
        sports_cats = {"sports", "esports", "football", "basketball",
                       "baseball", "hockey", "soccer", "tennis",
                       "golf", "mma", "boxing"}
        is_sport = cat_lower in sports_cats
        if is_sport and not self._s.allow_sports:
            return False, "sports disallowed"
        if not is_sport and category and not self._s.allow_non_sports:
            return False, "non-sports disallowed"

        # Time to expiry
        close_time_str = market.get("close_time", "")
        if close_time_str:
            try:
                close = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                days_left = (close - now).total_seconds() / 86400
                if days_left < 0:
                    return False, "already closed"
                if days_left > self._s.max_time_to_expiry_days:
                    return False, f"expires in {days_left:.0f}d (max {self._s.max_time_to_expiry_days}d)"
            except ValueError:
                pass

        # Volume / open interest — proxy for liquidity
        volume = _to_float(market.get("volume", 0))
        if volume < 10:
            return False, f"volume={volume} too low"

        # Yes price — skip near-certain outcomes
        yes_bid = _to_cents(market.get("yes_bid"))
        yes_ask = _to_cents(market.get("yes_ask"))

        if yes_bid is not None and yes_ask is not None:
            mid = (yes_bid + yes_ask) / 2
            if mid < 5 or mid > 95:
                return False, f"extreme price mid={mid}"

            spread = yes_ask - yes_bid
            if spread < self._s.min_spread_net_cents:
                return False, f"spread={spread}c < min {self._s.min_spread_net_cents}c"

        return True, "OK"


def _to_float(v) -> float:
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


def _to_cents(v) -> int | None:
    """Convert a Kalshi price to cents (1-99)."""
    if v is None:
        return None
    try:
        f = float(v)
        if f <= 1:  # dollar fraction
            return int(round(f * 100))
        return int(round(f))
    except (ValueError, TypeError):
        return None
