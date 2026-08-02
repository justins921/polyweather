"""
Fee model and net-edge profitability gate.

Every order MUST pass the net_edge gate before being placed.
Kalshi charges fees on the winning side at settlement; the model
supports configurable maker/taker rates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeeModel:
    """
    Configurable fee model.

    All values are in *dollars per contract*.
    Kalshi typically charges on settlement for winning contracts.
    We model entry + exit fees conservatively.
    """

    maker_fee: float = 0.01   # $ per contract when providing liquidity
    taker_fee: float = 0.03   # $ per contract when taking liquidity
    slippage_ticks: int = 2   # worst-case ticks of slippage (1 tick = $0.01)

    @property
    def slippage_dollars(self) -> float:
        return self.slippage_ticks * 0.01

    # ── Gross edge computation ───────────────────────────────────────────

    @staticmethod
    def half_spread_cents(best_bid: int, best_ask: int) -> float:
        """Half-spread in cents.  This is our theoretical gross capture."""
        return (best_ask - best_bid) / 2.0

    @staticmethod
    def spread_cents(best_bid: int, best_ask: int) -> int:
        return best_ask - best_bid

    # ── Net edge gate ────────────────────────────────────────────────────

    def net_edge_cents(
        self,
        best_bid: int,
        best_ask: int,
        *,
        is_maker: bool = True,
    ) -> float:
        """
        Compute net edge in cents for a round-trip (entry + exit).

        gross_edge   = spread / 2  (our theoretical capture as a passive MM)
        fees         = expected settlement fee (Kalshi charges winning side only)
        slippage     = configurable buffer (reduced for maker orders)
        net_edge     = gross_edge - fees - slippage

        If net_edge <= 0 the trade MUST be rejected.
        """
        spread = best_ask - best_bid
        gross = spread / 2.0

        per_side_fee = self.maker_fee if is_maker else self.taker_fee
        # Kalshi charges on the WINNING side at settlement only.
        # Expected fee = ~50% probability of winning * 1 fee per contract.
        expected_fee_cents = per_side_fee * 0.5 * 100  # convert $ → cents

        slip = self.slippage_ticks if not is_maker else max(self.slippage_ticks - 1, 0)

        net = gross - expected_fee_cents - slip
        return net

    def net_edge_dollars(
        self,
        best_bid: int,
        best_ask: int,
        *,
        is_maker: bool = True,
    ) -> float:
        """Net edge per contract in dollars."""
        return self.net_edge_cents(best_bid, best_ask, is_maker=is_maker) / 100.0

    def passes_gate(
        self,
        best_bid: int,
        best_ask: int,
        *,
        is_maker: bool = True,
    ) -> bool:
        """Return True iff the trade has positive net expectancy."""
        return self.net_edge_cents(best_bid, best_ask, is_maker=is_maker) > 0

    # ── Per-trade fee computation ────────────────────────────────────────

    def entry_fee(self, count: int, *, is_maker: bool = True) -> float:
        """Fee in dollars for entering `count` contracts."""
        per = self.maker_fee if is_maker else self.taker_fee
        return per * count

    def exit_fee(self, count: int, *, is_maker: bool = True) -> float:
        """Fee in dollars for exiting `count` contracts."""
        per = self.maker_fee if is_maker else self.taker_fee
        return per * count

    def round_trip_fee(self, count: int, *, is_maker: bool = True) -> float:
        return self.entry_fee(count, is_maker=is_maker) + self.exit_fee(
            count, is_maker=is_maker
        )

    # ── P&L breakdown ───────────────────────────────────────────────────

    def compute_pnl(
        self,
        entry_price_cents: int,
        exit_price_cents: int,
        count: int,
        side: str,
        *,
        entry_maker: bool = True,
        exit_maker: bool = True,
    ) -> dict[str, float]:
        """
        Compute gross and net P&L for a completed round-trip.

        Prices are in cents (1-99).  Side is 'yes' or 'no'.
        """
        if side == "yes":
            gross_per = (exit_price_cents - entry_price_cents) / 100.0
        else:
            gross_per = (entry_price_cents - exit_price_cents) / 100.0

        gross = gross_per * count
        fees = (
            self.entry_fee(count, is_maker=entry_maker)
            + self.exit_fee(count, is_maker=exit_maker)
        )
        net = gross - fees

        return {
            "gross_pnl": round(gross, 4),
            "fees": round(fees, 4),
            "net_pnl": round(net, 4),
            "count": count,
            "entry_cents": entry_price_cents,
            "exit_cents": exit_price_cents,
        }

    # ── Sizing helper ───────────────────────────────────────────────────

    @staticmethod
    def max_contracts(notional_usd: float, price_cents: int) -> int:
        """
        Max integer contracts purchasable for a given notional.

        Each contract costs `price_cents / 100` dollars.
        Always truncate (never round up) to stay within limits.
        """
        if price_cents <= 0 or price_cents >= 100:
            return 0
        cost_per = price_cents / 100.0
        return int(notional_usd / cost_per)
