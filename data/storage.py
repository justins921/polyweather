"""
Async SQLite storage for trades, quotes, fills, and P&L.

Uses aiosqlite for async-safe database access.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    strategy TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    pnl REAL,
    fees REAL DEFAULT 0.0,
    gross_pnl REAL,
    net_pnl REAL
);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    bid INTEGER NOT NULL,
    ask INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mid INTEGER NOT NULL,
    net_edge_cents REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    fee REAL NOT NULL DEFAULT 0.0,
    is_maker INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    gross_pnl REAL NOT NULL DEFAULT 0.0,
    fees REAL NOT NULL DEFAULT 0.0,
    net_pnl REAL NOT NULL DEFAULT 0.0,
    trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    max_drawdown REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);
CREATE INDEX IF NOT EXISTS idx_quotes_ts ON quotes(ts);
"""


class Storage:
    """Async SQLite storage layer."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_buffer: list[tuple[str, tuple]] = []

    async def init(self) -> None:
        """Open database and create schema."""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(path))
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Storage initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self.flush()
            await self._db.close()

    # ── Write operations ─────────────────────────────────────────────────

    async def log_trade(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        strategy: str = "",
        reason: str = "",
        pnl: float | None = None,
        fees: float = 0.0,
        gross_pnl: float | None = None,
        net_pnl: float | None = None,
    ) -> None:
        sql = """INSERT INTO trades
            (ts, ticker, side, count, price_cents, strategy, reason, pnl, fees, gross_pnl, net_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        self._write_buffer.append((sql, (
            time.time(), ticker, side, count, price_cents,
            strategy, reason, pnl, fees, gross_pnl, net_pnl,
        )))
        if len(self._write_buffer) >= 10:
            await self.flush()

    async def log_quote(
        self,
        ticker: str,
        bid: int,
        ask: int,
        size: int,
        mid: int,
        net_edge_cents: float,
    ) -> None:
        sql = """INSERT INTO quotes (ts, ticker, bid, ask, size, mid, net_edge_cents)
                 VALUES (?, ?, ?, ?, ?, ?, ?)"""
        self._write_buffer.append((sql, (
            time.time(), ticker, bid, ask, size, mid, net_edge_cents,
        )))

    async def log_fill(
        self,
        order_id: str,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        fee: float,
        is_maker: bool = True,
    ) -> None:
        sql = """INSERT INTO fills (ts, order_id, ticker, side, count, price_cents, fee, is_maker)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        self._write_buffer.append((sql, (
            time.time(), order_id, ticker, side, count, price_cents, fee, int(is_maker),
        )))

    async def flush(self) -> None:
        """Write buffered operations to disk."""
        if not self._write_buffer or not self._db:
            return
        try:
            for sql, params in self._write_buffer:
                await self._db.execute(sql, params)
            await self._db.commit()
            self._write_buffer.clear()
        except Exception as exc:
            logger.error("Storage flush failed: %s", exc)

    # ── Read operations ──────────────────────────────────────────────────

    async def get_trades(
        self, ticker: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not self._db:
            return []
        if ticker:
            cursor = await self._db.execute(
                "SELECT * FROM trades WHERE ticker=? ORDER BY ts DESC LIMIT ?",
                (ticker, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in await cursor.fetchall()]

    async def get_daily_pnl(self) -> dict[str, float]:
        """Return net P&L grouped by date."""
        if not self._db:
            return {}
        cursor = await self._db.execute("""
            SELECT date(ts, 'unixepoch') as day, SUM(COALESCE(net_pnl, pnl, 0))
            FROM trades
            GROUP BY day ORDER BY day
        """)
        return {row[0]: row[1] for row in await cursor.fetchall()}

    async def get_stats(self) -> dict[str, Any]:
        """Return aggregate trading statistics."""
        if not self._db:
            return {}
        cursor = await self._db.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN COALESCE(net_pnl, pnl, 0) > 0 THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(gross_pnl, 0)) as gross_pnl,
                SUM(COALESCE(fees, 0)) as total_fees,
                SUM(COALESCE(net_pnl, pnl, 0)) as net_pnl
            FROM trades
        """)
        row = await cursor.fetchone()
        if not row:
            return {}
        return {
            "total_trades": row[0],
            "wins": row[1] or 0,
            "win_rate": (row[1] or 0) / max(row[0], 1),
            "gross_pnl": round(row[2] or 0, 4),
            "total_fees": round(row[3] or 0, 4),
            "net_pnl": round(row[4] or 0, 4),
        }
