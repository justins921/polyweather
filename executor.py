"""
Trade execution via Polymarket CLOB API using py-clob-client.
"""

import logging
from typing import Any

import config

logger = logging.getLogger(__name__)


def get_clob_client():
    """
    Initialize and return an authenticated ClobClient.

    Lazy import so the bot can run in dry-run mode without py-clob-client
    or valid credentials.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        logger.error(
            "py-clob-client not installed. Run: pip install py-clob-client"
        )
        return None

    if not config.PRIVATE_KEY:
        logger.error("PRIVATE_KEY not set in config.py — cannot trade")
        return None

    client = ClobClient(
        config.CLOB_API_URL,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
    )

    # Derive API credentials (signs a message with the wallet key)
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
    except Exception as e:
        logger.error("Failed to derive API credentials: %s", e)
        return None

    logger.info("CLOB client authenticated successfully")
    return client


def execute_trade(
    client,
    token_id: str,
    side: str,
    size_usd: float,
    market_price: float,
) -> dict[str, Any]:
    """
    Place a limit order on Polymarket.

    Args:
        client: Authenticated ClobClient
        token_id: CLOB token ID for the outcome to buy
        side: "BUY" (we always buy — either YES or NO token)
        size_usd: Dollar amount to spend
        market_price: Current market price — we place limit at this price

    Returns:
        dict with trade result info
    """
    try:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY
    except ImportError:
        return {"success": False, "error": "py-clob-client not installed"}

    # Calculate size in shares: shares = usd_amount / price
    if market_price <= 0 or market_price >= 1:
        return {"success": False, "error": f"Invalid market price: {market_price}"}

    shares = size_usd / market_price

    try:
        order_args = OrderArgs(
            price=round(market_price, 2),
            size=round(shares, 2),
            side=BUY,
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order)

        logger.info(
            "Order placed: %s shares @ $%.2f = $%.2f | result: %s",
            shares,
            market_price,
            size_usd,
            result,
        )

        return {
            "success": True,
            "order_id": result.get("orderID", result.get("id", "")),
            "price": market_price,
            "shares": round(shares, 2),
            "size_usd": size_usd,
            "raw_response": result,
        }

    except Exception as e:
        logger.error("Trade execution failed: %s", e)
        return {"success": False, "error": str(e)}


def get_open_positions(client) -> list[dict[str, Any]]:
    """Fetch current open orders/positions."""
    if client is None:
        return []

    try:
        # Get open orders
        open_orders = client.get_orders()
        if isinstance(open_orders, list):
            return open_orders
        return []
    except Exception as e:
        logger.warning("Failed to fetch open positions: %s", e)
        return []


def get_balance(client) -> float | None:
    """Fetch current USDC balance from the CLOB client."""
    if client is None:
        return None

    try:
        balance_info = client.get_balance_allowance()
        if isinstance(balance_info, dict):
            # Balance is returned in wei-like units, convert
            raw = float(balance_info.get("balance", 0))
            # py-clob-client returns balance in USDC base units (6 decimals)
            return raw / 1e6
        return None
    except Exception as e:
        logger.warning("Failed to fetch balance: %s", e)
        return None
