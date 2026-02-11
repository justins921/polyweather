"""
Trade execution via Kalshi REST API using RSA-PSS authentication.
"""

import base64
import datetime
import logging
import uuid
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

# Lazy-loaded key objects
_private_key = None


def _load_private_key():
    """Load the Kalshi RSA private key from the configured PEM file."""
    global _private_key
    if _private_key is not None:
        return _private_key

    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        logger.error("cryptography not installed. Run: pip install cryptography")
        return None

    if not config.KALSHI_PRIVATE_KEY_PATH:
        logger.error("KALSHI_PRIVATE_KEY_PATH not set in config.py")
        return None

    try:
        with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            _private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
        return _private_key
    except FileNotFoundError:
        logger.error("Kalshi private key file not found: %s", config.KALSHI_PRIVATE_KEY_PATH)
        return None
    except Exception as e:
        logger.error("Failed to load Kalshi private key: %s", e)
        return None


def _sign_request(method: str, path: str) -> dict[str, str]:
    """
    Create authentication headers for a Kalshi API request.

    Signs: {timestamp}{METHOD}{path_without_query_params}
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    pk = _load_private_key()
    if pk is None:
        raise RuntimeError("Cannot sign request: private key not loaded")

    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))

    # Strip query params for signing
    path_for_signing = path.split("?")[0]
    message = f"{timestamp}{method}{path_for_signing}".encode("utf-8")

    signature = pk.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


def _get_base_url() -> str:
    """Return the correct Kalshi API base URL."""
    base = config.KALSHI_DEMO_BASE if config.KALSHI_DEMO_MODE else config.KALSHI_API_BASE
    return base


def _api_get(path: str) -> dict:
    """Authenticated GET request to Kalshi API."""
    headers = _sign_request("GET", path)
    url = _get_base_url() + path
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _api_post(path: str, data: dict) -> dict:
    """Authenticated POST request to Kalshi API."""
    headers = _sign_request("POST", path)
    url = _get_base_url() + path
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_kalshi_client():
    """
    Verify Kalshi credentials are valid by loading the key and checking balance.

    Returns True if credentials work, None on failure.
    Replaces the old get_clob_client() — Kalshi uses per-request signing
    instead of a persistent client object.
    """
    if not config.KALSHI_API_KEY_ID:
        logger.error("KALSHI_API_KEY_ID not set in config.py — cannot trade")
        return None

    pk = _load_private_key()
    if pk is None:
        return None

    # Test the credentials with a balance check
    try:
        balance = get_balance(None)
        if balance is not None:
            logger.info("Kalshi authenticated successfully (balance: $%.2f)", balance)
            return True
        else:
            logger.error("Kalshi auth check failed: could not fetch balance")
            return None
    except Exception as e:
        logger.error("Kalshi authentication failed: %s", e)
        return None


# Alias so bot.py can call get_clob_client() without changes
get_clob_client = get_kalshi_client


def execute_trade(
    client,
    token_id: str,
    side: str,
    size_usd: float,
    market_price: float,
) -> dict[str, Any]:
    """
    Place a limit order on Kalshi.

    Args:
        client: Ignored (Kalshi uses per-request signing, not a client object)
        token_id: Kalshi market ticker (e.g. "KXHIGHNY-26FEB11-B35")
        side: "BUY" (we always buy — either YES or NO contracts)
        size_usd: Dollar amount to spend
        market_price: Current market price (0-1 scale) — we place limit at this price

    Returns:
        dict with trade result info
    """
    if market_price <= 0 or market_price >= 1:
        return {"success": False, "error": f"Invalid market price: {market_price}"}

    # Kalshi prices are in cents (1-99)
    price_cents = max(1, min(99, round(market_price * 100)))

    # Number of contracts: each contract pays $1 if YES, costs price_cents
    # size_usd / (price_cents / 100) = number of contracts
    count = max(1, int(size_usd / (price_cents / 100)))

    # Map the side parameter to Kalshi's yes/no
    # bot.py passes "YES" or "NO" as the side
    kalshi_side = side.lower() if side.lower() in ("yes", "no") else "yes"

    try:
        order_data = {
            "ticker": token_id,
            "action": "buy",
            "side": kalshi_side,
            "count": count,
            "type": "limit",
            "yes_price": price_cents,
            "client_order_id": str(uuid.uuid4()),
        }

        result = _api_post(f"{config.KALSHI_API_PATH}/portfolio/orders", order_data)
        order = result.get("order", {})

        logger.info(
            "Order placed: %d contracts @ $%.2f = $%.2f | order_id: %s",
            count,
            price_cents / 100,
            size_usd,
            order.get("order_id", ""),
        )

        return {
            "success": True,
            "order_id": order.get("order_id", ""),
            "price": price_cents / 100,
            "shares": count,
            "size_usd": size_usd,
            "raw_response": result,
        }

    except Exception as e:
        logger.error("Trade execution failed: %s", e)
        return {"success": False, "error": str(e)}


def get_open_positions(client) -> list[dict[str, Any]]:
    """Fetch current open positions from Kalshi."""
    try:
        data = _api_get(f"{config.KALSHI_API_PATH}/portfolio/positions")
        positions = data.get("market_positions", [])
        # Filter to only positions with non-zero holdings
        return [p for p in positions if _parse_float(p.get("position", 0)) != 0]
    except Exception as e:
        logger.warning("Failed to fetch open positions: %s", e)
        return []


def get_balance(client) -> float | None:
    """Fetch current USD balance from Kalshi (balance is in cents)."""
    try:
        data = _api_get(f"{config.KALSHI_API_PATH}/portfolio/balance")
        # Kalshi returns balance in cents
        raw = float(data.get("balance", 0))
        return raw / 100
    except Exception as e:
        logger.warning("Failed to fetch balance: %s", e)
        return None


def _parse_float(value) -> float:
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0
