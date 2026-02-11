#!/usr/bin/env python3
"""
Web dashboard for the Polymarket Weather Trading Bot.

Serves a real-time monitoring dashboard that reads from the bot's
JSONL log files and displays stats, charts, and activity.

Usage:
    python dashboard.py                 # Start on port 5050
    python dashboard.py --port 8080     # Custom port
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

import config

app = Flask(__name__, static_folder="static")
log = logging.getLogger(__name__)


# ── Helpers to parse JSONL log files ────────────────────────────────────────


def _read_jsonl(filepath: str) -> list[dict]:
    """Read all records from a JSONL file."""
    records = []
    if not os.path.exists(filepath):
        return records
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _read_log_tail(filepath: str, n: int = 200) -> list[str]:
    """Read the last N lines from a text log file."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip() for l in lines[-n:]]


# ── API endpoints ───────────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


def _compute_stats(cycles: list[dict], analyses: list[dict]) -> dict:
    """Compute trading stats from a set of cycles and analyses."""
    bankroll = config.STARTING_BANKROLL
    if cycles:
        bankroll = cycles[-1].get("bankroll", bankroll)

    starting_bankroll = config.STARTING_BANKROLL
    if cycles:
        starting_bankroll = cycles[0].get("bankroll", starting_bankroll)

    executed_trades = [
        a for a in analyses
        if a.get("trade_result") and a["trade_result"].get("success")
        and not a["trade_result"].get("dry_run")
    ]
    dry_run_trades = [
        a for a in analyses
        if a.get("trade_result") and a["trade_result"].get("dry_run")
    ]

    all_trades_with_decision = executed_trades + dry_run_trades
    total_trades = len(all_trades_with_decision)
    wins = 0
    losses = 0
    for a in all_trades_with_decision:
        bet = a.get("bet_decision", {})
        if bet.get("should_trade"):
            if bet.get("edge", 0) > 0:
                wins += 1
            else:
                losses += 1
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

    bet_sizes = []
    edges = []
    for a in analyses:
        bet = a.get("bet_decision", {})
        if bet.get("should_trade"):
            sz = bet.get("size_usd", 0)
            if sz > 0:
                bet_sizes.append(sz)
            e = bet.get("edge", 0)
            if e > 0:
                edges.append(e)

    avg_bet = sum(bet_sizes) / len(bet_sizes) if bet_sizes else 0.0
    best_edge = max(edges) if edges else 0.0
    worst_edge = min(edges) if edges else 0.0
    avg_edge = sum(edges) / len(edges) if edges else 0.0

    total_markets_scanned = sum(c.get("markets_found", 0) for c in cycles)

    # Estimate API costs from token usage in analysis records
    api_calls = 0
    api_cost = 0.0
    for a in analyses:
        ana = a.get("analysis", {})
        in_tok = ana.get("input_tokens", 0)
        out_tok = ana.get("output_tokens", 0)
        if in_tok or out_tok:
            api_calls += 1
            api_cost += (
                in_tok * config.CLAUDE_INPUT_COST_PER_MTOK / 1_000_000
                + out_tok * config.CLAUDE_OUTPUT_COST_PER_MTOK / 1_000_000
            )

    # Trade cost from executed trades
    trade_cost = sum(
        a.get("bet_decision", {}).get("size_usd", 0)
        for a in executed_trades
    )

    return {
        "bankroll": round(bankroll, 2),
        "starting_bankroll": round(starting_bankroll, 2),
        "api_cost": round(api_cost, 4),
        "api_calls": api_calls,
        "win_rate": round(win_rate, 1),
        "total_trades": total_trades,
        "live_trades": len(executed_trades),
        "dry_run_trades": len(dry_run_trades),
        "total_markets_scanned": total_markets_scanned,
        "avg_bet_size": round(avg_bet, 2),
        "best_edge": round(best_edge, 4),
        "worst_edge": round(worst_edge, 4),
        "avg_edge": round(avg_edge, 4),
        "trade_cost": round(trade_cost, 4),
        "pnl": round(-trade_cost - api_cost, 4),  # negative until positions resolve
        "cycles": len(cycles),
    }


@app.route("/api/status")
def api_status():
    """Current bot status with both session and lifetime stats."""
    all_records = _read_jsonl(config.TRADE_LOG_FILE)

    # Find the latest session start
    session_starts = [r for r in all_records if r.get("type") == "session_start"]
    latest_session_id = session_starts[-1].get("session_id") if session_starts else None

    # Split records into session vs all
    all_cycles = [r for r in all_records if r.get("type") == "scan_cycle"]
    all_analyses = [r for r in all_records if r.get("type") == "analysis"]

    if latest_session_id:
        sess_cycles = [r for r in all_cycles if r.get("session_id") == latest_session_id]
        sess_analyses = [r for r in all_analyses if r.get("session_id") == latest_session_id]
    else:
        sess_cycles = all_cycles
        sess_analyses = all_analyses

    session = _compute_stats(sess_cycles, sess_analyses)
    lifetime = _compute_stats(all_cycles, all_analyses)

    # Current bankroll from latest cycle overall
    bankroll = config.STARTING_BANKROLL
    if all_cycles:
        bankroll = all_cycles[-1].get("bankroll", bankroll)

    return jsonify({
        "bankroll": round(bankroll, 2),
        "hard_stop": config.HARD_STOP_BANKROLL,
        "max_positions": config.MAX_OPEN_POSITIONS,
        "daily_api_budget": config.DAILY_API_BUDGET,
        "starting_bankroll": config.STARTING_BANKROLL,
        "session": session,
        "lifetime": lifetime,
    })


@app.route("/api/history")
def api_history():
    """Balance history over time for the chart."""
    trades = _read_jsonl(config.TRADE_LOG_FILE)
    cycles = [r for r in trades if r.get("type") == "scan_cycle"]

    history = []
    for c in cycles:
        history.append({
            "timestamp": c.get("timestamp", ""),
            "bankroll": c.get("bankroll", config.STARTING_BANKROLL),
            "cycle": c.get("cycle", 0),
        })

    # If no cycles yet, add starting point
    if not history:
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bankroll": config.STARTING_BANKROLL,
            "cycle": 0,
        })

    return jsonify(history)


@app.route("/api/activity")
def api_activity():
    """Recent activity log entries."""
    lines = _read_log_tail(config.BOT_LOG_FILE, n=150)

    # Parse log lines into structured entries
    entries = []
    for line in reversed(lines):
        # Format: "YYYY-MM-DD HH:MM:SS [LEVEL  ] module: message"
        entry = {"raw": line}
        if "] " in line and ": " in line:
            try:
                ts_and_level = line.split("] ", 1)
                ts_part = ts_and_level[0]
                rest = ts_and_level[1]

                # Extract timestamp
                bracket_idx = ts_part.rfind("[")
                timestamp = ts_part[:bracket_idx].strip()
                level = ts_part[bracket_idx + 1:].strip()

                # Extract module and message
                colon_idx = rest.find(": ")
                if colon_idx >= 0:
                    module = rest[:colon_idx].strip()
                    message = rest[colon_idx + 2:]
                else:
                    module = ""
                    message = rest

                entry = {
                    "timestamp": timestamp,
                    "level": level,
                    "module": module,
                    "message": message,
                    "raw": line,
                }
            except (IndexError, ValueError):
                pass

        entries.append(entry)

    return jsonify(entries[:100])


@app.route("/api/trades")
def api_trades():
    """All trade analysis records."""
    trades = _read_jsonl(config.TRADE_LOG_FILE)
    analyses = [r for r in trades if r.get("type") == "analysis"]

    # Return most recent first, limited
    result = []
    for a in reversed(analyses[-50:]):
        bet = a.get("bet_decision", {})
        tr = a.get("trade_result")
        result.append({
            "timestamp": a.get("timestamp", ""),
            "market": a.get("market_question", ""),
            "should_trade": bet.get("should_trade", False),
            "side": bet.get("side", ""),
            "size_usd": bet.get("size_usd", 0),
            "edge": bet.get("edge", 0),
            "reason": bet.get("reason", ""),
            "trade_success": tr.get("success") if tr else None,
            "dry_run": tr.get("dry_run", False) if tr else False,
        })

    return jsonify(result)


@app.route("/api/events")
def api_events():
    """Recent scan cycle summaries."""
    trades = _read_jsonl(config.TRADE_LOG_FILE)
    cycles = [r for r in trades if r.get("type") == "scan_cycle"]

    result = []
    for c in reversed(cycles[-20:]):
        result.append({
            "timestamp": c.get("timestamp", ""),
            "cycle": c.get("cycle", 0),
            "markets_found": c.get("markets_found", 0),
            "markets_analyzed": c.get("markets_analyzed", 0),
            "trades_attempted": c.get("trades_attempted", 0),
            "trades_executed": c.get("trades_executed", 0),
            "bankroll": c.get("bankroll", 0),
        })

    return jsonify(result)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polyweather Dashboard")
    parser.add_argument("--port", type=int, default=5050, help="Port to serve on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    os.makedirs(config.LOG_DIR, exist_ok=True)

    print(f"Dashboard running at http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
