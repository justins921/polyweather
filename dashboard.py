#!/usr/bin/env python3
"""
Web dashboard for the Kalshi Trading Bot.

Reads from SQLite (data/trading.db) and structured JSON logs (logs/bot.jsonl).
Surfaces: P&L breakdown (gross/fees/net), exposure, risk state,
per-market contribution, strategy attribution, circuit breakers,
kill-switch status, and a live activity feed.

Usage:
    python dashboard.py                 # Start on port 5050
    python dashboard.py --port 8080     # Custom port
    python dashboard.py --db data/trading.db  # Custom DB path
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")
log = logging.getLogger(__name__)

# Defaults — overridden by CLI args
DB_PATH = "data/trading.db"
LOG_FILE = "logs/bot.jsonl"
STARTING_BANKROLL = 81.09

# ── SQLite helpers (sync — Flask is sync) ─────────────────────────────────


def _get_db() -> sqlite3.Connection:
    """Open a read-only connection to the trading database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query and return list of dicts."""
    try:
        db = _get_db()
        cur = db.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        db.close()
        return rows
    except Exception as e:
        log.warning("DB query failed: %s", e)
        return []


def _query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


# ── JSON log helpers ──────────────────────────────────────────────────────


def _read_log_tail(filepath: str, n: int = 300) -> list[dict]:
    """Read last N lines from a JSONL log, parse each as dict."""
    entries = []
    if not os.path.exists(filepath):
        return entries
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines[-n:]:
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"msg": line, "level": "RAW"})
    return entries


# ── API: Status / Overview ────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


@app.route("/api/status")
def api_status():
    """Core stats: P&L, fees, win rate, exposure, risk state."""
    stats = _query_one("""
        SELECT
            COUNT(*)                                          AS total_trades,
            SUM(CASE WHEN COALESCE(net_pnl, pnl, 0) > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(gross_pnl, 0))                      AS gross_pnl,
            SUM(COALESCE(fees, 0))                            AS total_fees,
            SUM(COALESCE(net_pnl, pnl, 0))                   AS net_pnl,
            MAX(ts)                                           AS last_trade_ts
        FROM trades
    """) or {}

    today_stats = _query_one("""
        SELECT
            COUNT(*)                                          AS trades_today,
            SUM(COALESCE(net_pnl, pnl, 0))                   AS pnl_today,
            SUM(COALESCE(fees, 0))                            AS fees_today
        FROM trades
        WHERE date(ts, 'unixepoch') = date('now')
    """) or {}

    total_trades = stats.get("total_trades", 0) or 0
    wins = stats.get("wins", 0) or 0

    # Count distinct active tickers (proxy for open markets)
    active = _query_one("""
        SELECT COUNT(DISTINCT ticker) AS cnt
        FROM trades
        WHERE ts > unixepoch('now', '-1 hour')
    """) or {}

    # Quotes in last hour
    quote_stats = _query_one("""
        SELECT COUNT(*) AS total_quotes,
               AVG(net_edge_cents) AS avg_edge
        FROM quotes
        WHERE ts > unixepoch('now', '-1 hour')
    """) or {}

    # Check for kill switch / circuit breaker signals in logs
    risk_state = _parse_risk_state_from_logs()

    return jsonify({
        "bankroll": STARTING_BANKROLL + (stats.get("net_pnl") or 0),
        "starting_bankroll": STARTING_BANKROLL,
        "gross_pnl": round(stats.get("gross_pnl") or 0, 4),
        "total_fees": round(stats.get("total_fees") or 0, 4),
        "net_pnl": round(stats.get("net_pnl") or 0, 4),
        "total_trades": total_trades,
        "wins": wins,
        "win_rate": round(wins / max(total_trades, 1), 4),
        "last_trade_ts": stats.get("last_trade_ts"),
        "today": {
            "trades": today_stats.get("trades_today") or 0,
            "pnl": round(today_stats.get("pnl_today") or 0, 4),
            "fees": round(today_stats.get("fees_today") or 0, 4),
        },
        "active_markets": active.get("cnt") or 0,
        "quotes_last_hour": quote_stats.get("total_quotes") or 0,
        "avg_edge_cents": round(quote_stats.get("avg_edge") or 0, 2),
        "risk": risk_state,
    })


def _parse_risk_state_from_logs() -> dict:
    """Scan recent log entries for kill-switch / circuit breaker events."""
    state = {
        "killed": False,
        "kill_reason": "",
        "circuit_breakers": [],
        "daily_loss_hit": False,
    }
    entries = _read_log_tail(LOG_FILE, n=100)
    for e in reversed(entries):
        msg = e.get("msg", "")
        action = e.get("action", "")
        if action == "kill_switch" or "KILL SWITCH" in msg:
            state["killed"] = True
            state["kill_reason"] = e.get("reason", msg)
        if action == "circuit_breaker" or "CIRCUIT BREAKER" in msg:
            ticker = e.get("ticker", "")
            reason = e.get("reason", msg)
            state["circuit_breakers"].append({"ticker": ticker, "reason": reason})
        # Only the actual pause message counts — routine cycle summaries
        # also contain the substring "daily_loss" and must not trigger it.
        if "Daily loss limit hit" in msg:
            state["daily_loss_hit"] = True
    # Deduplicate breakers
    seen = set()
    deduped = []
    for b in state["circuit_breakers"]:
        key = b["ticker"]
        if key not in seen:
            seen.add(key)
            deduped.append(b)
    state["circuit_breakers"] = deduped[-10:]
    return state


# ── API: Per-market P&L breakdown ─────────────────────────────────────────


@app.route("/api/markets")
def api_markets():
    """P&L and trade count broken down by market ticker."""
    rows = _query("""
        SELECT
            ticker,
            strategy,
            COUNT(*)                            AS trades,
            SUM(COALESCE(gross_pnl, 0))         AS gross_pnl,
            SUM(COALESCE(fees, 0))              AS fees,
            SUM(COALESCE(net_pnl, pnl, 0))      AS net_pnl,
            SUM(CASE WHEN COALESCE(net_pnl, pnl, 0) > 0 THEN 1 ELSE 0 END) AS wins
        FROM trades
        GROUP BY ticker, strategy
        ORDER BY net_pnl DESC
    """)
    return jsonify(rows)


# ── API: Daily P&L history ───────────────────────────────────────────────


@app.route("/api/history")
def api_history():
    """Daily P&L time series for the equity chart."""
    rows = _query("""
        SELECT
            date(ts, 'unixepoch') AS day,
            SUM(COALESCE(gross_pnl, 0)) AS gross,
            SUM(COALESCE(fees, 0)) AS fees,
            SUM(COALESCE(net_pnl, pnl, 0)) AS net
        FROM trades
        GROUP BY day ORDER BY day
    """)

    # Build cumulative equity curve
    equity = STARTING_BANKROLL
    series = [{"day": "start", "equity": round(equity, 4), "net": 0}]
    for r in rows:
        daily_net = r.get("net") or 0
        equity += daily_net
        series.append({
            "day": r["day"],
            "equity": round(equity, 4),
            "gross": round(r.get("gross") or 0, 4),
            "fees": round(r.get("fees") or 0, 4),
            "net": round(daily_net, 4),
        })
    return jsonify(series)


# ── API: Recent trades ───────────────────────────────────────────────────


@app.route("/api/trades")
def api_trades():
    """Recent trade records."""
    rows = _query("""
        SELECT id, ts, ticker, side, count, price_cents,
               strategy, reason, pnl, fees, gross_pnl, net_pnl
        FROM trades
        ORDER BY ts DESC
        LIMIT 50
    """)
    for r in rows:
        r["time"] = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime(
            "%m/%d %H:%M:%S"
        ) if r.get("ts") else ""
    return jsonify(rows)


# ── API: Recent quotes ──────────────────────────────────────────────────


@app.route("/api/quotes")
def api_quotes():
    """Recent quote placements."""
    rows = _query("""
        SELECT ts, ticker, bid, ask, size, mid, net_edge_cents
        FROM quotes
        ORDER BY ts DESC
        LIMIT 50
    """)
    for r in rows:
        r["time"] = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime(
            "%m/%d %H:%M:%S"
        ) if r.get("ts") else ""
        r["spread"] = (r.get("ask") or 0) - (r.get("bid") or 0)
    return jsonify(rows)


# ── API: Activity log ───────────────────────────────────────────────────


@app.route("/api/activity")
def api_activity():
    """Structured JSON log entries for the activity feed."""
    entries = _read_log_tail(LOG_FILE, n=200)

    results = []
    for e in reversed(entries):
        level = e.get("level", "INFO")
        msg = e.get("msg", "")
        ts = e.get("ts", "")

        # Classify
        cls = "info"
        action = e.get("action", "")
        if action in ("kill_switch", "circuit_breaker") or level in ("ERROR", "CRITICAL"):
            cls = "error"
        elif "Fill" in msg or "Order placed" in msg or "EXECUTING" in msg:
            cls = "trade"
        elif "filtered" in msg.lower() or "skipping" in msg.lower() or "blocked" in msg.lower():
            cls = "skip"
        elif "cycle" in msg.lower() or "eligible" in msg.lower():
            cls = "scan"

        results.append({
            "ts": ts,
            "level": level,
            "msg": msg,
            "cls": cls,
            "ticker": e.get("ticker", ""),
            "action": action,
        })

    return jsonify(results[:100])


# ── API: Fills ───────────────────────────────────────────────────────────


@app.route("/api/fills")
def api_fills():
    """Recent order fills."""
    rows = _query("""
        SELECT ts, order_id, ticker, side, count, price_cents,
               fee, is_maker
        FROM fills
        ORDER BY ts DESC
        LIMIT 50
    """)
    for r in rows:
        r["time"] = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime(
            "%m/%d %H:%M:%S"
        ) if r.get("ts") else ""
    return jsonify(rows)


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    global DB_PATH, LOG_FILE, STARTING_BANKROLL

    parser = argparse.ArgumentParser(description="Kalshi Trading Dashboard")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", default="data/trading.db", help="Path to SQLite DB")
    parser.add_argument("--log-file", default="logs/bot.jsonl", help="Path to JSON log")
    parser.add_argument("--bankroll", type=float, default=81.09, help="Starting bankroll")
    args = parser.parse_args()

    DB_PATH = args.db
    LOG_FILE = args.log_file
    STARTING_BANKROLL = args.bankroll

    logging.basicConfig(level=logging.INFO)

    # Ensure DB directory exists (so SQLite can open even if empty)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    # Create tables if DB doesn't exist yet
    if not Path(DB_PATH).exists():
        _bootstrap_db()

    print(f"Dashboard: http://localhost:{args.port}")
    print(f"  DB:  {DB_PATH}")
    print(f"  Log: {LOG_FILE}")
    app.run(host=args.host, port=args.port, debug=False)


def _bootstrap_db():
    """Create an empty database with the schema so queries don't fail."""
    from data.storage import SCHEMA
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
