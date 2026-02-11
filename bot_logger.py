"""
Logging setup for the trading bot.

Provides:
- Human-readable log file (logs/bot.log) + console output
- Machine-readable JSONL trade log (logs/trades.jsonl)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import config


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the bot."""
    os.makedirs(config.LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console.setFormatter(console_fmt)
    root.addHandler(console)

    # File handler
    file_handler = logging.FileHandler(config.BOT_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    root.addHandler(file_handler)

    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


_current_session_id: str | None = None


def set_session_id(session_id: str) -> None:
    """Set the current session ID for all subsequent log records."""
    global _current_session_id
    _current_session_id = session_id


def log_trade(record: dict[str, Any]) -> None:
    """Append a trade record to the JSONL trade log."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    if _current_session_id:
        record["session_id"] = _current_session_id

    with open(config.TRADE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def log_session_start(mode: str, bankroll: float) -> str:
    """Log a session start marker. Returns the session ID."""
    import uuid
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    log_trade({
        "type": "session_start",
        "session_id": session_id,
        "mode": mode,
        "starting_bankroll": bankroll,
    })
    return session_id


def log_scan_cycle(
    cycle_num: int,
    markets_found: int,
    markets_analyzed: int,
    trades_attempted: int,
    trades_executed: int,
    bankroll: float,
) -> None:
    """Log a summary of a scan cycle to the JSONL file."""
    log_trade(
        {
            "type": "scan_cycle",
            "cycle": cycle_num,
            "markets_found": markets_found,
            "markets_analyzed": markets_analyzed,
            "trades_attempted": trades_attempted,
            "trades_executed": trades_executed,
            "bankroll": bankroll,
        }
    )


def log_analysis(
    market_question: str,
    analysis: dict[str, Any],
    bet_decision: dict[str, Any],
    trade_result: dict[str, Any] | None = None,
) -> None:
    """Log a full analysis + decision record."""
    log_trade(
        {
            "type": "analysis",
            "market_question": market_question,
            "analysis": analysis,
            "bet_decision": bet_decision,
            "trade_result": trade_result,
        }
    )
