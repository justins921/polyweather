"""
Polymarket weather market scanner.

Uses the Gamma API to discover active weather markets,
then fetches live prices from the CLOB API.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)


def fetch_weather_events() -> list[dict[str, Any]]:
    """
    Fetch active weather events from the Gamma API.

    Returns parsed events, each containing its list of markets (outcome buckets).
    Polymarket weather events typically have multiple markets per event,
    e.g. "Highest temperature in NYC on Feb 11?" has 7 bucket markets.
    """
    url = f"{config.GAMMA_API_URL}/events"
    params = {
        "tag_slug": config.MARKET_TAG,
        "active": "true",
        "closed": "false",
        "limit": 100,
    }
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        raw_events = resp.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch events from Gamma API: %s", e)
        return []

    events = []
    for raw in raw_events:
        parsed = _parse_event(raw)
        if parsed:
            events.append(parsed)

    logger.info("Fetched %d weather events (%d total markets)", len(events), sum(len(e["markets"]) for e in events))
    return events


def _parse_event(raw_event: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a raw Gamma API event into our internal format."""
    title = raw_event.get("title", "")
    slug = raw_event.get("slug", "")
    raw_markets = raw_event.get("markets", [])

    if not raw_markets:
        return None

    markets = []
    for m in raw_markets:
        parsed = _parse_market(m)
        if parsed:
            markets.append(parsed)

    if not markets:
        return None

    # Use the first market's end date as the event end date
    end_date = markets[0].get("end_date")
    days_to_resolution = markets[0].get("days_to_resolution")

    # Filter: skip events that resolve too far out
    if days_to_resolution is not None and days_to_resolution > config.MAX_DAYS_TO_RESOLUTION:
        logger.debug("Skipping event '%s' — resolves in %.0f days", title, days_to_resolution)
        return None

    # Total liquidity across all markets in the event
    total_liquidity = sum(m["liquidity"] for m in markets)

    return {
        "event_title": title,
        "event_slug": slug,
        "markets": markets,
        "end_date": end_date,
        "days_to_resolution": days_to_resolution,
        "total_liquidity": total_liquidity,
        "total_volume": sum(m["volume"] for m in markets),
        # Build a summary of all outcomes and prices for Claude
        "outcome_summary": [
            {
                "question": m["question"],
                "outcomes": m["outcomes"],
                "outcome_prices": m["outcome_prices"],
                "condition_id": m["condition_id"],
                "clob_token_ids": m["clob_token_ids"],
                "liquidity": m["liquidity"],
            }
            for m in markets
        ],
    }


def _parse_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a single market dict from the Gamma API response."""
    try:
        # outcomePrices and clobTokenIds may be JSON strings or lists
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        clob_token_ids = market.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        if not outcomes or not outcome_prices or not clob_token_ids:
            return None

        prices = [float(p) for p in outcome_prices]

        # Parse end date
        end_date_str = market.get("endDate") or market.get("end_date_iso")
        end_date = None
        days_to_resolution = None
        if end_date_str:
            end_date_str = end_date_str.replace("Z", "+00:00")
            try:
                end_date = datetime.fromisoformat(end_date_str)
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                days_to_resolution = (end_date - now).total_seconds() / 86400
                if days_to_resolution < 0:
                    return None
            except ValueError:
                pass

        liquidity = float(market.get("liquidity", 0) or 0)

        return {
            "condition_id": market.get("conditionId", ""),
            "question_id": market.get("questionId", ""),
            "question": market.get("question", ""),
            "description": market.get("description", ""),
            "outcomes": outcomes,
            "outcome_prices": prices,
            "clob_token_ids": clob_token_ids,
            "liquidity": liquidity,
            "volume": float(market.get("volume", 0) or 0),
            "end_date": end_date.isoformat() if end_date else None,
            "days_to_resolution": days_to_resolution,
            "market_slug": market.get("slug", ""),
        }

    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning("Failed to parse market: %s — %s", market.get("question", "?"), e)
        return None


def fetch_clob_prices(token_id: str) -> dict[str, Any] | None:
    """Fetch midpoint price and order book summary from the CLOB API."""
    result: dict[str, Any] = {"token_id": token_id}

    # Midpoint
    try:
        resp = requests.get(
            f"{config.CLOB_API_URL}/midpoint",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        result["midpoint"] = float(data.get("mid", 0))
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to fetch midpoint for %s: %s", token_id, e)
        result["midpoint"] = None

    # Order book (top-of-book only)
    try:
        resp = requests.get(
            f"{config.CLOB_API_URL}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        book = resp.json()

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        result["best_bid"] = float(bids[0]["price"]) if bids else None
        result["best_ask"] = float(asks[0]["price"]) if asks else None
        result["bid_size"] = float(bids[0].get("size", 0)) if bids else None
        result["ask_size"] = float(asks[0].get("size", 0)) if asks else None
    except (requests.RequestException, ValueError, IndexError, KeyError) as e:
        logger.warning("Failed to fetch book for %s: %s", token_id, e)
        result["best_bid"] = None
        result["best_ask"] = None

    return result
