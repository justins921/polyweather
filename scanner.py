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


def fetch_weather_markets() -> list[dict[str, Any]]:
    """Fetch active weather events from the Gamma API and flatten into markets."""
    url = f"{config.GAMMA_API_URL}/events"
    params = {
        "tag": config.MARKET_TAG,
        "active": "true",
        "closed": "false",
        "limit": 100,
    }
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        events = resp.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch events from Gamma API: %s", e)
        return []

    markets = []
    for event in events:
        event_title = event.get("title", "")
        event_slug = event.get("slug", "")
        for market in event.get("markets", []):
            parsed = _parse_market(market, event_title, event_slug)
            if parsed:
                markets.append(parsed)

    logger.info("Fetched %d weather markets from %d events", len(markets), len(events))
    return markets


def _parse_market(
    market: dict[str, Any], event_title: str, event_slug: str
) -> dict[str, Any] | None:
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

        # Need at least YES/NO with prices and token IDs
        if not outcomes or not outcome_prices or not clob_token_ids:
            return None

        # Parse prices as floats
        prices = [float(p) for p in outcome_prices]

        # Parse end date
        end_date_str = market.get("endDate") or market.get("end_date_iso")
        end_date = None
        if end_date_str:
            # Handle various ISO formats
            end_date_str = end_date_str.replace("Z", "+00:00")
            try:
                end_date = datetime.fromisoformat(end_date_str)
            except ValueError:
                pass

        # Check days to resolution
        if end_date:
            now = datetime.now(timezone.utc)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            days_to_resolution = (end_date - now).total_seconds() / 86400
            if days_to_resolution > config.MAX_DAYS_TO_RESOLUTION:
                logger.debug(
                    "Skipping market '%s' — resolves in %.0f days",
                    market.get("question", ""),
                    days_to_resolution,
                )
                return None
            if days_to_resolution < 0:
                return None
        else:
            days_to_resolution = None

        # Liquidity filter
        liquidity = float(market.get("liquidity", 0) or 0)
        if liquidity < config.MIN_LIQUIDITY:
            logger.debug(
                "Skipping market '%s' — liquidity $%.0f below threshold",
                market.get("question", ""),
                liquidity,
            )
            return None

        return {
            "condition_id": market.get("conditionId", ""),
            "question_id": market.get("questionId", ""),
            "question": market.get("question", ""),
            "description": market.get("description", ""),
            "event_title": event_title,
            "event_slug": event_slug,
            "outcomes": outcomes,
            "outcome_prices": prices,
            "clob_token_ids": clob_token_ids,
            "liquidity": liquidity,
            "volume": float(market.get("volume", 0) or 0),
            "end_date": end_date.isoformat() if end_date else None,
            "days_to_resolution": days_to_resolution,
            "market_slug": market.get("slug", ""),
            "active": market.get("active", True),
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


def enrich_market_with_clob(market: dict[str, Any]) -> dict[str, Any]:
    """Add live CLOB price data to a market dict."""
    clob_data = {}
    for i, token_id in enumerate(market.get("clob_token_ids", [])):
        outcome = market["outcomes"][i] if i < len(market["outcomes"]) else f"outcome_{i}"
        prices = fetch_clob_prices(token_id)
        if prices:
            clob_data[outcome] = prices
    market["clob_prices"] = clob_data
    return market
