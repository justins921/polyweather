#!/usr/bin/env python3
"""
Polymarket Weather Trading Bot

Scans Polymarket for mispriced weather prediction markets,
fetches real forecast data, uses Claude to estimate true probabilities,
and auto-executes trades using Kelly Criterion sizing.

Usage:
    python bot.py                  # Live auto-trading loop
    python bot.py --dry-run        # Analyze but don't execute trades
    python bot.py --once           # Single scan cycle then exit
    python bot.py --dry-run --once # Cheapest test: one cycle, no trades
    python bot.py --verbose        # Debug-level logging
"""

import argparse
import logging
import signal
import sys
import time
from typing import Any

import config
from analyzer import AnalysisResult, analyze_market
from bot_logger import log_analysis, log_scan_cycle, setup_logging
from cost_tracker import AnalysisCache, CostTracker
from executor import execute_trade, get_balance, get_clob_client, get_open_positions
from scanner import enrich_market_with_clob, fetch_weather_markets
from sizing import calculate_bet
from weather import fetch_weather_for_market

logger = logging.getLogger(__name__)

# Global flag for graceful shutdown
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s — shutting down after current cycle", signum)
    _shutdown = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Weather Trading Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze markets but don't execute trades",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan cycle then exit",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    return parser.parse_args()


def _prefilter_market(market: dict[str, Any]) -> str | None:
    """
    Quick checks to skip markets before spending an API call.

    Returns a skip reason string, or None if market passes.
    """
    prices = market.get("outcome_prices", [])
    if not prices:
        return "no prices"

    price_yes = prices[0]

    # Markets near 0 or 1 are already decided — no edge to find
    if price_yes < config.SKIP_EXTREME_PRICE_THRESHOLD:
        return f"price too low ({price_yes:.2f})"
    if price_yes > (1 - config.SKIP_EXTREME_PRICE_THRESHOLD):
        return f"price too high ({price_yes:.2f})"

    return None


def run_cycle(
    cycle_num: int,
    dry_run: bool,
    clob_client: Any,
    bankroll: float,
    open_position_count: int,
    cost_tracker: CostTracker,
    cache: AnalysisCache,
) -> float:
    """
    Run one full scan -> analyze -> size -> trade cycle.

    Returns the updated bankroll.
    """
    logger.info("=" * 60)
    logger.info(
        "CYCLE %d — Bankroll: $%.2f — Open positions: %d — API today: $%.4f/$%.2f",
        cycle_num, bankroll, open_position_count,
        cost_tracker.daily_cost, config.DAILY_API_BUDGET,
    )
    logger.info("=" * 60)

    # ── Step 1: Scan for weather markets ──────────────────────────────────
    logger.info("Step 1: Scanning Polymarket for weather markets...")
    markets = fetch_weather_markets()
    if not markets:
        logger.info("No weather markets found. Waiting for next cycle.")
        log_scan_cycle(cycle_num, 0, 0, 0, 0, bankroll)
        return bankroll

    logger.info("Found %d weather markets passing filters", len(markets))

    # ── Step 2-5: Process each market ─────────────────────────────────────
    markets_analyzed = 0
    trades_attempted = 0
    trades_executed = 0

    for market in markets:
        if _shutdown:
            logger.info("Shutdown requested, stopping market processing")
            break

        question = market.get("question", "Unknown")
        condition_id = market.get("condition_id", "")
        logger.info("-" * 50)
        logger.info("Market: %s", question)

        # Check if we've hit position limits
        if open_position_count >= config.MAX_OPEN_POSITIONS:
            logger.info("Max open positions reached (%d), skipping remaining", open_position_count)
            break

        # Check hard stop
        if bankroll <= config.HARD_STOP_BANKROLL:
            logger.warning(
                "HARD STOP: Bankroll $%.2f <= $%.2f threshold. Pausing.",
                bankroll, config.HARD_STOP_BANKROLL,
            )
            break

        # ── Pre-filter (free, no API call) ────────────────────────────────
        skip_reason = _prefilter_market(market)
        if skip_reason:
            logger.info("  Pre-filter skip: %s", skip_reason)
            cost_tracker.calls_skipped_prefilter += 1
            continue

        # Step 2: Fetch weather data
        logger.info("  Fetching weather forecast...")
        weather_data = fetch_weather_for_market(market)
        if weather_data is None:
            logger.info("  No weather data available. Skipping.")
            cost_tracker.calls_skipped_prefilter += 1
            log_analysis(question, {}, {"should_trade": False, "reason": "No weather data"})
            continue

        # ── Check analysis cache ──────────────────────────────────────────
        market_price_yes = market["outcome_prices"][0] if market["outcome_prices"] else 0.5
        cached = cache.get(condition_id, market_price_yes)
        if cached is not None:
            logger.info("  Using cached analysis (price hasn't moved enough)")
            cost_tracker.calls_skipped_cache += 1
            analysis = AnalysisResult(**cached)
        else:
            # ── Check daily API budget ────────────────────────────────────
            if cost_tracker.is_budget_exceeded():
                logger.warning(
                    "  Daily API budget $%.2f exceeded ($%.4f spent). Skipping Claude call.",
                    config.DAILY_API_BUDGET, cost_tracker.daily_cost,
                )
                cost_tracker.calls_skipped_budget += 1
                continue

            # Enrich with live CLOB prices
            logger.info("  Fetching live CLOB prices...")
            market = enrich_market_with_clob(market)

            # Step 3: Claude analysis
            logger.info("  Analyzing with Claude...")
            analysis = analyze_market(market, weather_data, cost_tracker)
            if analysis is None:
                logger.warning("  Claude analysis failed. Skipping.")
                log_analysis(question, {}, {"should_trade": False, "reason": "Analysis failed"})
                continue

            # Cache the result
            cache.put(condition_id, market_price_yes, analysis.to_dict())

        markets_analyzed += 1
        logger.info(
            "  Claude estimate: fair_value_yes=%.2f, confidence=%.2f",
            analysis.fair_value_yes, analysis.confidence,
        )
        logger.info("  Reasoning: %s", analysis.reasoning)

        # Step 4: Bet sizing
        bet = calculate_bet(
            fair_value_yes=analysis.fair_value_yes,
            confidence=analysis.confidence,
            market_price_yes=market_price_yes,
            bankroll=bankroll,
            open_positions=open_position_count,
        )

        logger.info("  Sizing decision: %s", bet.reason)

        if not bet.should_trade:
            log_analysis(question, analysis.to_dict(), bet.to_dict())
            continue

        trades_attempted += 1

        # Determine which token to buy
        if bet.side == "YES":
            token_id = market["clob_token_ids"][0]
            buy_price = market_price_yes
        else:
            token_id = market["clob_token_ids"][1] if len(market["clob_token_ids"]) > 1 else None
            buy_price = 1 - market_price_yes

        if not token_id:
            logger.warning("  No token ID for %s side. Skipping.", bet.side)
            log_analysis(question, analysis.to_dict(), bet.to_dict())
            continue

        # Step 5: Execute trade
        trade_result = None
        if dry_run:
            logger.info(
                "  [DRY RUN] Would buy %s @ $%.2f for $%.2f",
                bet.side, buy_price, bet.size_usd,
            )
            trade_result = {"success": True, "dry_run": True}
        else:
            if clob_client is None:
                logger.error("  No CLOB client — cannot execute trade")
                trade_result = {"success": False, "error": "No CLOB client"}
            else:
                logger.info(
                    "  EXECUTING: Buy %s @ $%.2f for $%.2f",
                    bet.side, buy_price, bet.size_usd,
                )
                trade_result = execute_trade(
                    client=clob_client,
                    token_id=token_id,
                    side="BUY",
                    size_usd=bet.size_usd,
                    market_price=buy_price,
                )

                if trade_result.get("success"):
                    trades_executed += 1
                    open_position_count += 1
                    bankroll -= bet.size_usd
                    cost_tracker.record_trade(bet.size_usd)
                    logger.info("  Trade executed successfully!")
                else:
                    logger.error("  Trade failed: %s", trade_result.get("error"))

        log_analysis(question, analysis.to_dict(), bet.to_dict(), trade_result)

    # ── Cycle summary ─────────────────────────────────────────────────────
    log_scan_cycle(
        cycle_num, len(markets), markets_analyzed, trades_attempted, trades_executed, bankroll
    )
    logger.info("=" * 60)
    logger.info(
        "Cycle %d complete: %d found, %d analyzed, %d trades attempted, %d executed",
        cycle_num, len(markets), markets_analyzed, trades_attempted, trades_executed,
    )

    # Cost & P&L summary
    cost_tracker.log_summary()
    cost_tracker.save_to_ledger()

    # Evict stale cache entries
    evicted = cache.evict_expired()
    if evicted:
        logger.debug("Evicted %d stale cache entries (%d remaining)", evicted, cache.size)

    return bankroll


def main():
    args = parse_args()
    setup_logging(verbose=args.verbose)

    logger.info("Polymarket Weather Trading Bot starting")
    logger.info(
        "Mode: %s%s",
        "DRY RUN" if args.dry_run else "LIVE",
        " (single cycle)" if args.once else "",
    )

    # Validate configuration
    if not args.dry_run and not config.PRIVATE_KEY:
        logger.error("PRIVATE_KEY not set in config.py — cannot run in live mode")
        logger.error("Use --dry-run to test without trading")
        sys.exit(1)

    if not config.CLAUDE_API_KEY:
        logger.error("CLAUDE_API_KEY not set in config.py")
        sys.exit(1)

    # Initialize CLOB client for live mode
    clob_client = None
    if not args.dry_run:
        logger.info("Initializing CLOB client...")
        clob_client = get_clob_client()
        if clob_client is None:
            logger.error("Failed to initialize CLOB client. Exiting.")
            sys.exit(1)

    # Set up graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Initialize cost tracker and analysis cache
    cost_tracker = CostTracker()
    cache = AnalysisCache()

    # Initial bankroll
    bankroll = config.STARTING_BANKROLL
    if clob_client:
        balance = get_balance(clob_client)
        if balance is not None:
            bankroll = balance
            logger.info("Fetched wallet balance: $%.2f", bankroll)
        else:
            logger.info(
                "Could not fetch balance, using configured starting bankroll: $%.2f",
                bankroll,
            )

    cycle_num = 0

    while not _shutdown:
        cycle_num += 1

        # Count open positions
        open_positions = get_open_positions(clob_client) if clob_client else []
        open_position_count = len(open_positions)

        try:
            bankroll = run_cycle(
                cycle_num=cycle_num,
                dry_run=args.dry_run,
                clob_client=clob_client,
                bankroll=bankroll,
                open_position_count=open_position_count,
                cost_tracker=cost_tracker,
                cache=cache,
            )
        except Exception:
            logger.exception("Unhandled error in cycle %d", cycle_num)

        if args.once:
            logger.info("Single cycle complete. Exiting.")
            break

        if not _shutdown:
            logger.info(
                "Sleeping %d seconds until next cycle...",
                config.SCAN_INTERVAL_SECONDS,
            )
            # Sleep in short increments so we can respond to shutdown signals
            for _ in range(config.SCAN_INTERVAL_SECONDS):
                if _shutdown:
                    break
                time.sleep(1)

    # Final summary
    logger.info("Bot stopped. Final bankroll: $%.2f", bankroll)
    cost_tracker.log_summary()
    cost_tracker.save_to_ledger()


if __name__ == "__main__":
    main()
