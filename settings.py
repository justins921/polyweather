"""
Centralised pydantic-settings configuration.

Loads from .env, environment variables, and CLI overrides.
Every risk limit is typed + validated so nothing silently mis-configures.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Kalshi credentials ───────────────────────────────────────────────
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_demo_mode: bool = True
    kalshi_base_url: str = "https://api.elections.kalshi.com"
    kalshi_demo_url: str = "https://demo-api.kalshi.co"
    kalshi_api_path: str = "/trade-api/v2"
    kalshi_ws_path: str = "/trade-api/ws/v2"

    # ── Bankroll ─────────────────────────────────────────────────────────
    starting_bankroll: float = 100.0

    # ── Risk limits (NON-NEGOTIABLE) ─────────────────────────────────────
    max_daily_loss: float = Field(default=3.0, ge=0)
    max_total_exposure: float = Field(default=20.0, ge=0)
    max_concurrent_markets: int = Field(default=6, ge=1)
    max_per_market_notional: float = Field(default=5.0, ge=0)
    max_per_order_notional: float = Field(default=2.0, ge=0)

    # Kill switch
    kill_switch_error_pct: float = Field(default=5.0, ge=0)
    kill_switch_window_secs: int = Field(default=300, ge=30)
    kill_switch_ws_max_disconnects: int = Field(default=3, ge=1)
    kill_switch_ws_window_secs: int = Field(default=300, ge=60)

    # Circuit breaker per market
    circuit_breaker_adverse_ticks: int = Field(default=3, ge=1)
    circuit_breaker_window_secs: int = Field(default=10, ge=1)
    circuit_breaker_cooldown_secs: int = Field(default=300, ge=10)

    # ── Fee model ────────────────────────────────────────────────────────
    maker_fee_per_contract: float = Field(default=0.01, ge=0)
    taker_fee_per_contract: float = Field(default=0.03, ge=0)
    slippage_buffer_ticks: int = Field(default=2, ge=0)

    # ── Rate limits (token bucket) ───────────────────────────────────────
    read_rate_limit: int = Field(default=20, ge=1)
    write_rate_limit: int = Field(default=10, ge=1)

    # ── Market filter ────────────────────────────────────────────────────
    min_book_depth: int = Field(default=5, ge=1)
    min_spread_net_cents: int = Field(default=2, ge=0)
    max_time_to_expiry_days: int = Field(default=30, ge=1)

    # ── Strategy A: Market maker ─────────────────────────────────────────
    mm_quote_refresh_secs: float = Field(default=10.0, ge=1.0)
    mm_min_price_move_ticks: int = Field(default=1, ge=1)
    mm_adverse_move_ticks: int = Field(default=3, ge=1)
    mm_adverse_window_secs: int = Field(default=10, ge=1)
    mm_cooldown_secs: int = Field(default=300, ge=10)
    mm_skew_per_contract: float = Field(default=1.0, ge=0)
    mm_max_inventory: int = Field(default=10, ge=1)

    # ── Strategy B: Event reversion ──────────────────────────────────────
    er_rolling_window: int = Field(default=60, ge=10)
    er_entry_zscore: float = Field(default=2.0, ge=0.5)
    er_max_notional: float = Field(default=1.0, ge=0)
    er_stop_loss_ticks: int = Field(default=5, ge=1)
    er_take_profit_ticks: int = Field(default=3, ge=1)

    # ── Category allowlist ───────────────────────────────────────────────
    allow_sports: bool = True
    allow_non_sports: bool = True

    # ── Database ─────────────────────────────────────────────────────────
    db_path: str = "data/trading.db"

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = "logs/bot.jsonl"

    # ── Paper trading ────────────────────────────────────────────────────
    paper_mode: bool = False
    paper_latency_ms: int = Field(default=500, ge=0)

    # ── Derived ──────────────────────────────────────────────────────────

    @property
    def rest_base_url(self) -> str:
        base = self.kalshi_demo_url if self.kalshi_demo_mode else self.kalshi_base_url
        return base

    @property
    def rest_url(self) -> str:
        return self.rest_base_url + self.kalshi_api_path

    @property
    def ws_url(self) -> str:
        host = self.kalshi_demo_url if self.kalshi_demo_mode else self.kalshi_base_url
        scheme = host.replace("https://", "wss://").replace("http://", "ws://")
        return scheme + self.kalshi_ws_path

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()
