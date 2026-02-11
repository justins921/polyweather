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


@app.route("/api/status")
def api_status():
    """Current bot status: bankroll, P&L, API costs, win rate, key stats."""
    trades = _read_jsonl(config.TRADE_LOG_FILE)
    ledger = _read_jsonl(config.LEDGER_FILE)

    # Extract scan cycles and analysis records
    cycles = [r for r in trades if r.get("type") == "scan_cycle"]
    analyses = [r for r in trades if r.get("type") == "analysis"]

    # Current bankroll from latest cycle
    bankroll = config.STARTING_BANKROLL
    if cycles:
        bankroll = cycles[-1].get("bankroll", bankroll)

    # Latest ledger entry for cost data
    latest_cost = {}
    if ledger:
        latest_cost = ledger[-1]

    # Trade stats
    executed_trades = [
        a for a in analyses
        if a.get("trade_result") and a["trade_result"].get("success")
        and not a["trade_result"].get("dry_run")
    ]
    dry_run_trades = [
        a for a in analyses
        if a.get("trade_result") and a["trade_result"].get("dry_run")
    ]

    # Win rate: count all trades (live + dry run) with positive edge
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

    # Bet sizes
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
    best_trade = max(edges) if edges else 0.0
    worst_trade = min(edges) if edges else 0.0
    avg_edge = sum(edges) / len(edges) if edges else 0.0

    # Total markets scanned
    total_markets_scanned = sum(c.get("markets_found", 0) for c in cycles)
    total_events_analyzed = sum(c.get("markets_analyzed", 0) for c in cycles)

    # API costs from ledger (or estimate from analysis records if ledger is empty)
    api_cost_total = latest_cost.get("total_api_cost_usd", 0.0)
    api_cost_today = latest_cost.get("daily_api_cost_usd", 0.0)
    api_calls = latest_cost.get("api_calls_made", 0)
    net_pnl = latest_cost.get("net_pnl_usd", 0.0)
    trade_cost = latest_cost.get("total_trade_cost_usd", 0.0)
    trade_revenue = latest_cost.get("total_trade_revenue_usd", 0.0)

    # If ledger has no cost data, estimate from analysis records
    if api_calls == 0 and analyses:
        for a in analyses:
            ana = a.get("analysis", {})
            in_tok = ana.get("input_tokens", 0)
            out_tok = ana.get("output_tokens", 0)
            if in_tok or out_tok:
                api_calls += 1
                api_cost_total += (
                    in_tok * config.CLAUDE_INPUT_COST_PER_MTOK / 1_000_000
                    + out_tok * config.CLAUDE_OUTPUT_COST_PER_MTOK / 1_000_000
                )
        api_cost_today = api_cost_total

    # Session P&L = revenue - trade cost - api cost
    session_pnl = trade_revenue - trade_cost - api_cost_total

    return jsonify({
        "bankroll": round(bankroll, 2),
        "starting_bankroll": config.STARTING_BANKROLL,
        "session_pnl": round(session_pnl, 4),
        "api_cost_total": round(api_cost_total, 4),
        "api_cost_today": round(api_cost_today, 4),
        "api_calls": api_calls,
        "win_rate": round(win_rate, 1),
        "total_trades": total_trades,
        "live_trades": len(executed_trades),
        "dry_run_trades": len(dry_run_trades),
        "total_markets_scanned": total_markets_scanned,
        "total_events_analyzed": total_events_analyzed,
        "avg_bet_size": round(avg_bet, 2),
        "best_edge": round(best_trade, 4),
        "worst_edge": round(worst_trade, 4),
        "avg_edge": round(avg_edge, 4),
        "net_pnl": round(net_pnl, 4),
        "cycles_completed": len(cycles),
        "hard_stop": config.HARD_STOP_BANKROLL,
        "max_positions": config.MAX_OPEN_POSITIONS,
        "daily_api_budget": config.DAILY_API_BUDGET,
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
