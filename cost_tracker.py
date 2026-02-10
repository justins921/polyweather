"""
API cost tracking, analysis caching, and profitability ledger.

Tracks every Claude API call, caches results to avoid redundant calls,
and maintains a running P&L that includes API costs.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import config

logger = logging.getLogger(__name__)


class CostTracker:
    """Tracks Claude API costs and trading P&L."""

    def __init__(self):
        self.session_start = time.time()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_api_cost = 0.0
        self.total_trade_cost = 0.0  # money spent on trades
        self.total_trade_revenue = 0.0  # money received from resolved trades
        self.calls_made = 0
        self.calls_skipped_cache = 0
        self.calls_skipped_prefilter = 0
        self.calls_skipped_budget = 0
        self._daily_cost = 0.0
        self._daily_reset_date = datetime.now(timezone.utc).date()

    def record_api_call(self, input_tokens: int, output_tokens: int) -> float:
        """Record a Claude API call and return its cost in USD."""
        cost = (
            input_tokens * config.CLAUDE_INPUT_COST_PER_MTOK / 1_000_000
            + output_tokens * config.CLAUDE_OUTPUT_COST_PER_MTOK / 1_000_000
        )
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_api_cost += cost
        self.calls_made += 1

        # Daily tracking with auto-reset
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_cost = 0.0
            self._daily_reset_date = today
        self._daily_cost += cost

        return cost

    def record_trade(self, size_usd: float) -> None:
        """Record money spent on a trade."""
        self.total_trade_cost += size_usd

    def record_resolution(self, payout_usd: float) -> None:
        """Record money received from a resolved market."""
        self.total_trade_revenue += payout_usd

    def is_budget_exceeded(self) -> bool:
        """Check if daily API budget is exceeded."""
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_cost = 0.0
            self._daily_reset_date = today
        return self._daily_cost >= config.DAILY_API_BUDGET

    @property
    def daily_cost(self) -> float:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            return 0.0
        return self._daily_cost

    @property
    def net_pnl(self) -> float:
        """Net P&L including API costs."""
        return self.total_trade_revenue - self.total_trade_cost - self.total_api_cost

    @property
    def runtime_hours(self) -> float:
        return (time.time() - self.session_start) / 3600

    def summary(self) -> dict[str, Any]:
        return {
            "runtime_hours": round(self.runtime_hours, 2),
            "api_calls_made": self.calls_made,
            "api_calls_skipped_cache": self.calls_skipped_cache,
            "api_calls_skipped_prefilter": self.calls_skipped_prefilter,
            "api_calls_skipped_budget": self.calls_skipped_budget,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_api_cost_usd": round(self.total_api_cost, 4),
            "daily_api_cost_usd": round(self.daily_cost, 4),
            "daily_api_budget_usd": config.DAILY_API_BUDGET,
            "total_trade_cost_usd": round(self.total_trade_cost, 2),
            "total_trade_revenue_usd": round(self.total_trade_revenue, 2),
            "net_pnl_usd": round(self.net_pnl, 4),
            "cost_per_call_avg_usd": (
                round(self.total_api_cost / self.calls_made, 4)
                if self.calls_made > 0
                else 0
            ),
        }

    def log_summary(self) -> None:
        """Log a human-readable cost summary."""
        s = self.summary()
        logger.info("─── Cost & P&L Summary ───")
        logger.info(
            "  API: %d calls (%.0f cached, %.0f pre-filtered, %.0f budget-capped) "
            "│ $%.4f total │ $%.4f today │ $%.4f/call avg",
            s["api_calls_made"],
            s["api_calls_skipped_cache"],
            s["api_calls_skipped_prefilter"],
            s["api_calls_skipped_budget"],
            s["total_api_cost_usd"],
            s["daily_api_cost_usd"],
            s["cost_per_call_avg_usd"],
        )
        logger.info(
            "  Trading: $%.2f spent │ $%.2f revenue │ Net P&L: $%.4f (incl. API costs)",
            s["total_trade_cost_usd"],
            s["total_trade_revenue_usd"],
            s["net_pnl_usd"],
        )

    def save_to_ledger(self) -> None:
        """Append current summary to the ledger JSONL file."""
        os.makedirs(config.LOG_DIR, exist_ok=True)
        record = self.summary()
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        record["type"] = "cost_summary"
        with open(config.LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


class AnalysisCache:
    """
    In-memory cache of Claude analysis results.

    Avoids re-analyzing markets where the price hasn't moved significantly.
    Keyed by market condition_id.
    """

    def __init__(self):
        self._cache: dict[str, dict[str, Any]] = {}

    def get(
        self, condition_id: str, current_price_yes: float
    ) -> dict[str, Any] | None:
        """
        Return cached analysis if still valid, otherwise None.

        Cache is valid if:
        - Entry exists
        - TTL hasn't expired
        - Market price hasn't moved more than threshold
        """
        entry = self._cache.get(condition_id)
        if entry is None:
            return None

        # Check TTL
        age = time.time() - entry["cached_at"]
        if age > config.ANALYSIS_CACHE_TTL_SECONDS:
            del self._cache[condition_id]
            return None

        # Check price movement
        price_delta = abs(current_price_yes - entry["price_at_cache"])
        if price_delta > config.CACHE_PRICE_MOVE_THRESHOLD:
            del self._cache[condition_id]
            return None

        return entry["analysis"]

    def put(
        self,
        condition_id: str,
        current_price_yes: float,
        analysis: dict[str, Any],
    ) -> None:
        """Store an analysis result in the cache."""
        self._cache[condition_id] = {
            "cached_at": time.time(),
            "price_at_cache": current_price_yes,
            "analysis": analysis,
        }

    @property
    def size(self) -> int:
        return len(self._cache)

    def evict_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        now = time.time()
        expired = [
            k
            for k, v in self._cache.items()
            if now - v["cached_at"] > config.ANALYSIS_CACHE_TTL_SECONDS
        ]
        for k in expired:
            del self._cache[k]
        return len(expired)
