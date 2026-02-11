#!/usr/bin/env python3
"""
Async CLI entry point for the Kalshi trading bot.

Usage:
    python main.py                            # live (demo by default)
    python main.py --paper                    # paper trading sim
    python main.py --allow-sports false       # disable sports category
    python main.py --log-level DEBUG          # verbose logging
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time as _time
from typing import Any

from clients.kalshi_svc import KalshiClient
from data.storage import Storage
from engine.fee_calculator import FeeModel
from engine.market_filter import MarketFilter
from engine.paper_engine import PaperEngine
from engine.risk_manager import RiskManager
from log_config import setup_logging
from settings import Settings
from strategies.event_reversion import EventReversionStrategy
from strategies.market_maker import MarketMakerStrategy

logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kalshi micro-bankroll trading bot")
    p.add_argument("--paper", action="store_true", help="Paper-trading mode")
    p.add_argument("--allow-sports", type=_bool, default=None)
    p.add_argument("--allow-non-sports", type=_bool, default=None)
    p.add_argument("--log-level", type=str, default=None)
    p.add_argument("--demo", action="store_true", default=None,
                   help="Use Kalshi demo/sandbox environment")
    return p.parse_args()


def _bool(v: str) -> bool:
    return v.lower() in ("1", "true", "yes")


async def run(settings: Settings) -> None:
    """Main async run loop."""

    # ── Build components ─────────────────────────────────────────────────
    storage = Storage(settings.db_path)
    await storage.init()

    fee_model = FeeModel(
        maker_fee=settings.maker_fee_per_contract,
        taker_fee=settings.taker_fee_per_contract,
        slippage_ticks=settings.slippage_buffer_ticks,
    )
    risk_mgr = RiskManager(settings)
    mkt_filter = MarketFilter(settings)

    client: KalshiClient | PaperEngine
    if settings.paper_mode:
        live_client = KalshiClient(settings)
        await live_client.connect()
        client = PaperEngine(settings, live_client)
        logger.info("Running in PAPER mode (latency=%dms)", settings.paper_latency_ms)
    else:
        client = KalshiClient(settings)
        await client.connect()
        mode = "DEMO" if settings.kalshi_demo_mode else "LIVE"
        logger.info("Running in %s mode", mode)

    mm_strategy = MarketMakerStrategy(
        settings=settings,
        client=client,
        fee_model=fee_model,
        risk_mgr=risk_mgr,
        storage=storage,
    )
    er_strategy = EventReversionStrategy(
        settings=settings,
        client=client,
        fee_model=fee_model,
        risk_mgr=risk_mgr,
        storage=storage,
    )

    # ── Verify category availability ─────────────────────────────────────
    await _verify_categories(client, risk_mgr, settings)

    # ── Graceful shutdown ────────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # ── Core loop ────────────────────────────────────────────────────────
    logger.info(
        "Bot started  bankroll=$%.2f  max_daily_loss=$%.2f  max_exposure=$%.2f",
        settings.starting_bankroll,
        settings.max_daily_loss,
        settings.max_total_exposure,
    )

    # Parse series tickers for server-side filtering
    series_list: list[str] = [
        s.strip() for s in settings.series_tickers.split(",") if s.strip()
    ]
    if series_list:
        logger.info("Server-side series filter: %s", ", ".join(series_list))
    else:
        logger.warning(
            "No series_tickers configured — fetching ALL open markets every scan "
            "(set SERIES_TICKERS in .env to reduce API usage)"
        )

    # Market list cache
    cached_eligible: list[dict[str, Any]] = []
    last_scan_time = 0.0

    try:
        cycle = 0
        while not shutdown_event.is_set():
            cycle += 1

            # Check kill switch
            if risk_mgr.is_killed():
                logger.warning("Kill switch active: %s — waiting 60s", risk_mgr.kill_reason)
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=60)
                except asyncio.TimeoutError:
                    risk_mgr.maybe_reset_kill_switch()
                continue

            # Check daily loss limit
            if risk_mgr.daily_loss_exceeded():
                logger.warning(
                    "Daily loss limit hit ($%.2f/$%.2f) — pausing until midnight",
                    risk_mgr.daily_loss, settings.max_daily_loss,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=300)
                except asyncio.TimeoutError:
                    risk_mgr.maybe_reset_daily()
                continue

            # ── Market discovery (cached) ─────────────────────────────────
            now = _time.monotonic()
            scan_due = (now - last_scan_time) >= settings.market_scan_interval_secs

            if scan_due or not cached_eligible:
                try:
                    raw_markets = await _fetch_markets(client, series_list)
                except Exception as exc:
                    risk_mgr.record_api_error()
                    logger.error("Market fetch failed: %s", exc)
                    await asyncio.sleep(30)
                    continue

                risk_mgr.record_api_success()
                logger.info(
                    "Cycle %d: scanned %d markets from Kalshi",
                    cycle, len(raw_markets),
                )
                cached_eligible = mkt_filter.filter(raw_markets, risk_mgr)
                last_scan_time = now
            else:
                logger.info(
                    "Cycle %d: reusing cached market list (%d markets, next scan in %ds)",
                    cycle, len(cached_eligible),
                    int(settings.market_scan_interval_secs - (now - last_scan_time)),
                )

            eligible = cached_eligible

            if not eligible:
                logger.info("Cycle %d: no eligible markets — sleeping %ds",
                            cycle, 30)
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            logger.info("Cycle %d: processing %d eligible markets with both strategies",
                        cycle, len(eligible))

            # Run strategies concurrently
            tasks = []
            for mkt in eligible:
                tasks.append(mm_strategy.process_market(mkt))
                tasks.append(er_strategy.process_market(mkt))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = sum(1 for r in results if isinstance(r, Exception))
            for r in results:
                if isinstance(r, Exception):
                    logger.error("Strategy error: %s", r, exc_info=r)
                    risk_mgr.record_api_error()

            # Persist state
            await storage.flush()

            # Cycle summary
            risk_summary = risk_mgr.summary()
            logger.info(
                "Cycle %d done: daily_loss=$%.2f/%s%.2f  exposure=$%.2f/$%.2f  errors=%d  next_cycle=%ds",
                cycle,
                risk_summary.get("daily_loss", 0), "-" if risk_summary.get("daily_loss", 0) > 0 else "",
                settings.max_daily_loss,
                risk_summary.get("total_exposure", 0),
                settings.max_total_exposure,
                errors,
                int(settings.mm_quote_refresh_secs),
            )

            # Sleep between cycles
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=settings.mm_quote_refresh_secs,
                )
            except asyncio.TimeoutError:
                pass

    finally:
        logger.info("Shutting down — cancelling open orders...")
        try:
            await mm_strategy.cancel_all()
        except Exception as exc:
            logger.error("Error cancelling orders on shutdown: %s", exc)
        if hasattr(client, "close"):
            await client.close()
        await storage.close()
        logger.info("Shutdown complete")


async def _fetch_markets(
    client: Any, series_list: list[str]
) -> list[dict[str, Any]]:
    """Fetch markets, optionally scoped to specific series tickers."""
    if not series_list:
        return await client.get_active_markets()

    # Fetch each series in parallel — much cheaper than fetching everything
    tasks = [client.get_active_markets(series_ticker=s) for s in series_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_markets: list[dict[str, Any]] = []
    for series, result in zip(series_list, results):
        if isinstance(result, Exception):
            logger.warning("Failed to fetch series %s: %s", series, result)
        else:
            all_markets.extend(result)
    return all_markets


async def _verify_categories(
    client: Any, risk_mgr: RiskManager, settings: Settings
) -> None:
    """Check which market categories are actually tradable for this account."""
    try:
        markets = await client.get_active_markets()
        categories: set[str] = set()
        for m in markets:
            cat = m.get("category", "").lower()
            if cat:
                categories.add(cat)
        logger.info("Tradable categories detected: %s", categories)

        sports_cats = {"sports", "esports", "football", "basketball", "baseball",
                       "hockey", "soccer", "tennis", "golf", "mma", "boxing"}
        has_sports = bool(categories & sports_cats)
        has_non_sports = bool(categories - sports_cats)

        if settings.allow_sports and not has_sports:
            logger.warning("Sports markets requested but none found — disabling")
            risk_mgr.disable_category("sports")
        if settings.allow_non_sports and not has_non_sports:
            logger.warning("Non-sports markets requested but none found — disabling")
            risk_mgr.disable_category("non_sports")
    except Exception as exc:
        logger.warning("Category verification failed (will allow all): %s", exc)


def main() -> None:
    args = parse_args()
    settings = Settings()

    # CLI overrides
    if args.paper:
        settings.paper_mode = True
    if args.allow_sports is not None:
        settings.allow_sports = args.allow_sports
    if args.allow_non_sports is not None:
        settings.allow_non_sports = args.allow_non_sports
    if args.log_level is not None:
        settings.log_level = args.log_level.upper()
    if args.demo is not None:
        settings.kalshi_demo_mode = args.demo

    setup_logging(level=settings.log_level, log_file=settings.log_file)

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
