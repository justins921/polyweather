#!/usr/bin/env python3
"""
Kalshi Weather Trading Bot

Scans Kalshi for mispriced weather prediction markets,
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
from analyzer import EventAnalysis, analyze_event
from bot_logger import log_analysis, log_scan_cycle, setup_logging
from cost_tracker import AnalysisCache, CostTracker
from executor import execute_trade, get_balance, get_clob_client, get_open_positions
from scanner import fetch_weather_events
from sizing import calculate_bet
from weather import extract_city_from_question, fetch_weather_for_city

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


def _fetch_weather_for_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Extract city from event title or market questions and fetch weather."""
    # Try event title first, then individual market questions
    texts_to_try = [event.get("event_title", "")]
    for m in event.get("markets", []):
        texts_to_try.append(m.get("question", ""))

    for text in texts_to_try:
        match = extract_city_from_question(text)
        if match:
            city_name, (lat, lon, country_code) = match
            return fetch_weather_for_city(city_name, lat, lon, country_code)

    return None


def _find_best_trade(
    event: dict[str, Any],
    analysis: EventAnalysis,
    bankroll: float,
    open_position_count: int,
) -> dict[str, Any] | None:
    """
    Given an event analysis with fair values for all buckets,
    find the single best trade (largest edge) that passes all filters.
    """
    markets = event.get("markets", [])
    summaries = event.get("outcome_summary", [])
    buckets = analysis.buckets

    if len(buckets) != len(summaries):
        logger.warning(
            "  Bucket count mismatch: Claude returned %d, event has %d",
            len(buckets), len(summaries),
        )
        # Use minimum of the two to avoid index errors
        count = min(len(buckets), len(summaries))
    else:
        count = len(buckets)

    best = None
    best_edge = 0.0

    for i in range(count):
        fair_value = buckets[i].get("fair_value_yes", 0)
        market_price = summaries[i]["outcome_prices"][0] if summaries[i]["outcome_prices"] else 0.5

        bet = calculate_bet(
            fair_value_yes=fair_value,
            confidence=analysis.confidence,
            market_price_yes=market_price,
            bankroll=bankroll,
            open_positions=open_position_count,
        )

        if bet.should_trade and bet.edge > best_edge:
            best_edge = bet.edge
            best = {
                "market_index": i,
                "market": markets[i] if i < len(markets) else None,
                "summary": summaries[i],
                "bet": bet,
                "fair_value": fair_value,
                "market_price": market_price,
            }

    return best


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

    # ── Step 1: Scan for weather events ───────────────────────────────────
    logger.info("Step 1: Scanning Polymarket for weather events...")
    events = fetch_weather_events()
    if not events:
        logger.info("No weather events found. Waiting for next cycle.")
        log_scan_cycle(cycle_num, 0, 0, 0, 0, bankroll)
        return bankroll

    logger.info("Found %d weather events", len(events))

    # ── Step 2-5: Process each event ──────────────────────────────────────
    events_analyzed = 0
    trades_attempted = 0
    trades_executed = 0

    for event in events:
        if _shutdown:
            logger.info("Shutdown requested, stopping event processing")
            break

        title = event.get("event_title", "Unknown")
        slug = event.get("event_slug", "")
        n_markets = len(event.get("markets", []))
        logger.info("-" * 50)
        logger.info("Event: %s (%d buckets, $%.0f liq)", title, n_markets, event.get("total_liquidity", 0))

        # Check limits
        if open_position_count >= config.MAX_OPEN_POSITIONS:
            logger.info("Max open positions reached (%d), skipping remaining", open_position_count)
            break

        if bankroll <= config.HARD_STOP_BANKROLL:
            logger.warning("HARD STOP: Bankroll $%.2f <= $%.2f", bankroll, config.HARD_STOP_BANKROLL)
            break

        # Step 2: Fetch weather data
        logger.info("  Fetching weather forecast...")
        weather_data = _fetch_weather_for_event(event)
        if weather_data is None:
            logger.info("  No weather data (can't identify city). Skipping.")
            cost_tracker.calls_skipped_prefilter += 1
            continue

        # ── Check analysis cache (keyed by event slug) ────────────────────
        # Use the average YES price across buckets as the cache price signal
        all_yes_prices = [
            s["outcome_prices"][0]
            for s in event.get("outcome_summary", [])
            if s.get("outcome_prices")
        ]
        avg_price = sum(all_yes_prices) / len(all_yes_prices) if all_yes_prices else 0.5

        cached = cache.get(slug, avg_price)
        if cached is not None:
            logger.info("  Using cached analysis (prices haven't moved enough)")
            cost_tracker.calls_skipped_cache += 1
            analysis = EventAnalysis(**cached)
        else:
            # ── Check daily API budget ────────────────────────────────────
            if cost_tracker.is_budget_exceeded():
                logger.warning(
                    "  Daily API budget $%.2f exceeded ($%.4f spent). Skipping.",
                    config.DAILY_API_BUDGET, cost_tracker.daily_cost,
                )
                cost_tracker.calls_skipped_budget += 1
                continue

            # Step 3: Claude analysis (one call for all buckets)
            logger.info("  Analyzing %d buckets with Claude...", n_markets)
            analysis = analyze_event(event, weather_data, cost_tracker)
            if analysis is None:
                logger.warning("  Claude analysis failed. Skipping.")
                log_analysis(title, {}, {"should_trade": False, "reason": "Analysis failed"})
                continue

            # Cache the result
            cache.put(slug, avg_price, analysis.to_dict())

        events_analyzed += 1
        logger.info("  Confidence: %.2f", analysis.confidence)
        logger.info("  Reasoning: %s", analysis.reasoning)

        # Log Claude's fair values vs market
        for i, bucket in enumerate(analysis.buckets):
            summaries = event.get("outcome_summary", [])
            if i < len(summaries):
                mkt_price = summaries[i]["outcome_prices"][0] if summaries[i]["outcome_prices"] else "?"
                fair = bucket.get("fair_value_yes", 0)
                edge = fair - float(mkt_price) if isinstance(mkt_price, (int, float)) else 0
                marker = " <<<" if abs(edge) > config.MIN_EDGE_THRESHOLD else ""
                logger.info(
                    "    Bucket %d: fair=%.2f vs mkt=%.2f (edge=%+.2f)%s",
                    i + 1, fair, float(mkt_price), edge, marker,
                )

        # Step 4: Find the best trade across all buckets
        best = _find_best_trade(event, analysis, bankroll, open_position_count)

        if best is None:
            logger.info("  No tradeable edge found in any bucket.")
            log_analysis(title, analysis.to_dict(), {"should_trade": False, "reason": "No edge"})
            continue

        bet = best["bet"]
        summary = best["summary"]
        market = best["market"]
        trades_attempted += 1

        logger.info("  Best trade: %s", bet.reason)
        logger.info("  Bucket: %s", summary["question"][:80])

        # Determine trade parameters
        # On Kalshi, the ticker is the same for YES/NO — the side param picks which
        token_id = summary["clob_token_ids"][0] if summary["clob_token_ids"] else None
        if bet.side == "YES":
            buy_price = best["market_price"]
        else:
            buy_price = 1 - best["market_price"]

        if not token_id:
            logger.warning("  No ticker for trade. Skipping.")
            log_analysis(title, analysis.to_dict(), bet.to_dict())
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
                logger.error("  No Kalshi client — cannot execute trade")
                trade_result = {"success": False, "error": "No Kalshi client"}
            else:
                logger.info(
                    "  EXECUTING: Buy %s @ $%.2f for $%.2f",
                    bet.side, buy_price, bet.size_usd,
                )
                trade_result = execute_trade(
                    client=clob_client,
                    token_id=token_id,
                    side=bet.side,
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

        log_analysis(title, analysis.to_dict(), bet.to_dict(), trade_result)

    # ── Cycle summary ─────────────────────────────────────────────────────
    total_markets = sum(len(e.get("markets", [])) for e in events)
    log_scan_cycle(cycle_num, total_markets, events_analyzed, trades_attempted, trades_executed, bankroll)
    logger.info("=" * 60)
    logger.info(
        "Cycle %d complete: %d events (%d buckets), %d analyzed, %d trades attempted, %d executed",
        cycle_num, len(events), total_markets, events_analyzed, trades_attempted, trades_executed,
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

    logger.info("Kalshi Weather Trading Bot starting")
    logger.info(
        "Mode: %s%s",
        "DRY RUN" if args.dry_run else "LIVE",
        " (single cycle)" if args.once else "",
    )

    # Validate configuration
    if not args.dry_run and (not config.KALSHI_API_KEY_ID or not config.KALSHI_PRIVATE_KEY_PATH):
        logger.error("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in config.py for live mode")
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
