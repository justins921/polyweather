"""
Kalshi weather market scanner.

Uses the Kalshi REST API to discover active weather markets
across configured series (temperature, rain, etc.).
"""

import logging
from datetime import datetime, timezone
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    """Return the correct Kalshi API base URL based on demo mode."""
    base = config.KALSHI_DEMO_BASE if config.KALSHI_DEMO_MODE else config.KALSHI_API_BASE
    return base + config.KALSHI_API_PATH


def fetch_weather_events() -> list[dict[str, Any]]:
    """
    Fetch active weather events from Kalshi.

    Scans all configured weather series and returns parsed events,
    each containing its list of markets (outcome buckets).

    Kalshi hierarchy: Series → Events → Markets
    e.g. KXHIGHNY → KXHIGHNY-26FEB11 → KXHIGHNY-26FEB11-B35
    """
    base = _get_base_url()
    events = []

    for series_ticker in config.KALSHI_WEATHER_SERIES:
        try:
            series_events = _fetch_events_for_series(base, series_ticker)
            events.extend(series_events)
        except Exception as e:
            logger.warning("Failed to scan series %s: %s", series_ticker, e)

    logger.info(
        "Fetched %d weather events (%d total markets)",
        len(events),
        sum(len(e["markets"]) for e in events),
    )
    return events


def _fetch_events_for_series(
    base_url: str, series_ticker: str
) -> list[dict[str, Any]]:
    """Fetch all open markets for a series and group them into events."""
    url = f"{base_url}/markets"
    all_markets: list[dict] = []
    cursor = ""

    # Paginate through all markets in this series
    while True:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        markets = data.get("markets", [])
        all_markets.extend(markets)

        cursor = data.get("cursor", "")
        if not cursor or not markets:
            break

    if not all_markets:
        return []

    # Group markets by event_ticker
    events_map: dict[str, list[dict]] = {}
    for m in all_markets:
        event_ticker = m.get("event_ticker", "")
        if event_ticker:
            events_map.setdefault(event_ticker, []).append(m)

    # Parse each event group
    events = []
    for event_ticker, market_list in events_map.items():
        parsed = _parse_event(event_ticker, market_list)
        if parsed:
            events.append(parsed)

    return events


def _parse_event(
    event_ticker: str, raw_markets: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Parse a group of Kalshi markets into our internal event format."""
    if not raw_markets:
        return None

    markets = []
    for m in raw_markets:
        parsed = _parse_market(m)
        if parsed:
            markets.append(parsed)

    if not markets:
        return None

    # Derive event title from first market
    # Kalshi market titles look like "Highest temperature in NYC on Feb 11?"
    first = raw_markets[0]
    event_title = first.get("title", "") or first.get("event_ticker", event_ticker)
    # Try to get a cleaner event-level title
    subtitle = first.get("subtitle", "")
    if subtitle:
        # subtitle is usually the specific bucket, title is event-level
        event_title = first.get("title", event_title)

    # Use close_time from first market as event end date
    end_date = markets[0].get("end_date")
    days_to_resolution = markets[0].get("days_to_resolution")

    # Filter: skip events that resolve too far out
    if days_to_resolution is not None and days_to_resolution > config.MAX_DAYS_TO_RESOLUTION:
        logger.debug("Skipping event '%s' — resolves in %.0f days", event_title, days_to_resolution)
        return None

    # Total volume across all markets in the event
    total_volume = sum(m.get("volume", 0) for m in markets)

    return {
        "event_title": event_title,
        "event_slug": event_ticker,
        "markets": markets,
        "end_date": end_date,
        "days_to_resolution": days_to_resolution,
        "total_liquidity": total_volume,  # Kalshi uses volume/open_interest instead of liquidity pools
        "total_volume": total_volume,
        # Build outcome summary for Claude (same format the analyzer expects)
        "outcome_summary": [
            {
                "question": m["question"],
                "outcomes": m["outcomes"],
                "outcome_prices": m["outcome_prices"],
                "condition_id": m["ticker"],
                "clob_token_ids": [m["ticker"]],  # On Kalshi, the ticker IS the market ID
                "liquidity": m.get("volume", 0),
            }
            for m in markets
        ],
    }


def _parse_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a single Kalshi market into our internal format."""
    try:
        ticker = market.get("ticker", "")
        if not ticker:
            return None

        # Kalshi prices are in dollar strings (e.g. "0.5000") or cent integers
        yes_bid = _parse_price(market.get("yes_bid", market.get("yes_bid_dollars")))
        yes_ask = _parse_price(market.get("yes_ask", market.get("yes_ask_dollars")))
        last_price = _parse_price(market.get("last_price", market.get("last_price_dollars")))

        # Best estimate of YES price: midpoint of bid/ask, or last trade
        if yes_bid is not None and yes_ask is not None:
            yes_price = (yes_bid + yes_ask) / 2
        elif last_price is not None:
            yes_price = last_price
        else:
            yes_price = 0.5  # fallback

        no_price = 1.0 - yes_price

        # Question text
        question = market.get("title", "") or market.get("yes_sub_title", "") or ticker
        subtitle = market.get("yes_sub_title", "")
        if subtitle and subtitle not in question:
            question = f"{question} — {subtitle}"

        # Parse close time
        close_time_str = market.get("close_time", "")
        end_date = None
        days_to_resolution = None
        if close_time_str:
            try:
                end_date = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                days_to_resolution = (end_date - now).total_seconds() / 86400
                if days_to_resolution < 0:
                    return None  # already closed
            except ValueError:
                pass

        volume = _parse_float(market.get("volume", market.get("volume_fp", 0)))
        open_interest = _parse_float(market.get("open_interest", market.get("open_interest_fp", 0)))

        return {
            "ticker": ticker,
            "condition_id": ticker,
            "question_id": market.get("event_ticker", ""),
            "question": question,
            "description": market.get("rules_primary", ""),
            "outcomes": ["Yes", "No"],
            "outcome_prices": [yes_price, no_price],
            "clob_token_ids": [ticker],
            "liquidity": volume,
            "volume": volume,
            "open_interest": open_interest,
            "end_date": end_date.isoformat() if end_date else None,
            "days_to_resolution": days_to_resolution,
            "market_slug": ticker,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "last_price": last_price,
        }

    except (ValueError, TypeError) as e:
        logger.warning("Failed to parse Kalshi market: %s — %s", market.get("ticker", "?"), e)
        return None


def _parse_price(value) -> float | None:
    """Parse a Kalshi price value (could be dollar string, cents int, or None)."""
    if value is None:
        return None
    try:
        f = float(value)
        # If it looks like cents (> 1), convert to dollars
        if f > 1:
            return f / 100
        return f
    except (ValueError, TypeError):
        return None


def _parse_float(value) -> float:
    """Safely parse a float value."""
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def fetch_orderbook(ticker: str) -> dict[str, Any] | None:
    """Fetch orderbook for a Kalshi market."""
    base = _get_base_url()
    url = f"{base}/markets/{ticker}/orderbook"

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        book = data.get("orderbook", {})

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        return {
            "ticker": ticker,
            "best_yes_bid": yes_bids[0][0] / 100 if yes_bids else None,
            "best_yes_bid_size": yes_bids[0][1] if yes_bids else None,
            "best_no_bid": no_bids[0][0] / 100 if no_bids else None,
            "best_no_bid_size": no_bids[0][1] if no_bids else None,
        }
    except (requests.RequestException, ValueError, IndexError, KeyError) as e:
        logger.warning("Failed to fetch orderbook for %s: %s", ticker, e)
        return None
