"""
Backtester supporting CSV event playback and recorded orderbook snapshots.

Feed historical data through the strategy pipeline with the same
fee/risk gates as live and paper modes.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.fee_calculator import FeeModel

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    ts: float
    ticker: str
    side: str
    count: int
    entry_cents: int
    exit_cents: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    max_drawdown: float = 0.0
    per_market_pnl: dict[str, float] = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        return self.wins / max(self.total_trades, 1)

    def summary(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "gross_pnl": round(self.gross_pnl, 4),
            "total_fees": round(self.total_fees, 4),
            "net_pnl": round(self.net_pnl, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "per_market_pnl": {k: round(v, 4) for k, v in self.per_market_pnl.items()},
        }


class BacktestEngine:
    """
    Replay historical orderbook data through fee + risk logic.

    Input format (CSV):
        ts,ticker,best_bid,best_ask,volume

    Or JSONL snapshots:
        {"ts": ..., "ticker": ..., "yes": [[price, size], ...], "no": [[price, size], ...]}
    """

    def __init__(self, fee_model: FeeModel) -> None:
        self._fee = fee_model

    def run_csv(self, path: str | Path) -> BacktestResult:
        """Run backtest from a CSV file of BBO snapshots."""
        result = BacktestResult()
        equity = 0.0
        peak = 0.0

        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                bid = int(row["best_bid"])
                ask = int(row["best_ask"])
                ticker = row["ticker"]
                ts = float(row["ts"])

                if not self._fee.passes_gate(bid, ask, is_maker=True):
                    continue

                edge = self._fee.net_edge_cents(bid, ask, is_maker=True)
                pnl = self._fee.compute_pnl(
                    entry_price_cents=bid + 1,
                    exit_price_cents=ask - 1,
                    count=1,
                    side="yes",
                )

                trade = BacktestTrade(
                    ts=ts,
                    ticker=ticker,
                    side="yes",
                    count=1,
                    entry_cents=bid + 1,
                    exit_cents=ask - 1,
                    gross_pnl=pnl["gross_pnl"],
                    fees=pnl["fees"],
                    net_pnl=pnl["net_pnl"],
                )
                result.trades.append(trade)

                if trade.net_pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1

                result.gross_pnl += trade.gross_pnl
                result.total_fees += trade.fees
                result.net_pnl += trade.net_pnl
                result.per_market_pnl[ticker] = (
                    result.per_market_pnl.get(ticker, 0) + trade.net_pnl
                )

                equity += trade.net_pnl
                peak = max(peak, equity)
                dd = peak - equity
                result.max_drawdown = max(result.max_drawdown, dd)

        return result

    def run_jsonl(self, path: str | Path) -> BacktestResult:
        """Run backtest from a JSONL file of orderbook snapshots."""
        result = BacktestResult()
        equity = 0.0
        peak = 0.0

        with open(path) as f:
            for line in f:
                snap = json.loads(line)
                yes_bids = snap.get("yes", [])
                no_bids = snap.get("no", [])
                if not yes_bids or not no_bids:
                    continue

                bid = int(yes_bids[0][0])
                ask = 100 - int(no_bids[0][0])
                ticker = snap.get("ticker", "?")
                ts = float(snap.get("ts", 0))

                if bid >= ask or bid <= 0 or ask >= 100:
                    continue

                if not self._fee.passes_gate(bid, ask, is_maker=True):
                    continue

                pnl = self._fee.compute_pnl(
                    entry_price_cents=bid + 1,
                    exit_price_cents=ask - 1,
                    count=1,
                    side="yes",
                )

                trade = BacktestTrade(
                    ts=ts, ticker=ticker, side="yes", count=1,
                    entry_cents=bid + 1, exit_cents=ask - 1,
                    gross_pnl=pnl["gross_pnl"], fees=pnl["fees"],
                    net_pnl=pnl["net_pnl"],
                )
                result.trades.append(trade)

                if trade.net_pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1

                result.gross_pnl += trade.gross_pnl
                result.total_fees += trade.fees
                result.net_pnl += trade.net_pnl
                result.per_market_pnl[ticker] = (
                    result.per_market_pnl.get(ticker, 0) + trade.net_pnl
                )

                equity += trade.net_pnl
                peak = max(peak, equity)
                result.max_drawdown = max(result.max_drawdown, peak - equity)

        return result
