"""
Bet sizing using Kelly Criterion with risk management guardrails.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)


@dataclass
class BetDecision:
    """Result of the sizing calculation."""

    should_trade: bool
    side: str  # "YES" or "NO"
    size_usd: float
    edge: float
    kelly_fraction: float
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_trade": self.should_trade,
            "side": self.side,
            "size_usd": round(self.size_usd, 2),
            "edge": round(self.edge, 4),
            "kelly_fraction": round(self.kelly_fraction, 6),
            "reason": self.reason,
            "details": self.details,
        }


def calculate_bet(
    fair_value_yes: float,
    confidence: float,
    market_price_yes: float,
    bankroll: float,
    open_positions: int,
) -> BetDecision:
    """
    Determine whether to trade and how much, using Kelly Criterion.

    For binary markets:
        Kelly fraction f* = (p*b - q) / b
        where p = fair probability, q = 1 - p, b = payout odds

    We use quarter-Kelly scaled by confidence.
    """
    # ── Pre-checks ────────────────────────────────────────────────────────
    if bankroll <= config.HARD_STOP_BANKROLL:
        return BetDecision(
            should_trade=False,
            side="",
            size_usd=0,
            edge=0,
            kelly_fraction=0,
            reason=f"Hard stop: bankroll ${bankroll:.2f} <= ${config.HARD_STOP_BANKROLL:.2f}",
        )

    if open_positions >= config.MAX_OPEN_POSITIONS:
        return BetDecision(
            should_trade=False,
            side="",
            size_usd=0,
            edge=0,
            kelly_fraction=0,
            reason=f"Max positions reached: {open_positions}/{config.MAX_OPEN_POSITIONS}",
        )

    if confidence < config.MIN_CONFIDENCE:
        return BetDecision(
            should_trade=False,
            side="",
            size_usd=0,
            edge=0,
            kelly_fraction=0,
            reason=f"Confidence {confidence:.2f} below minimum {config.MIN_CONFIDENCE}",
        )

    # ── Determine side and edge ───────────────────────────────────────────
    edge_yes = fair_value_yes - market_price_yes
    edge_no = (1 - fair_value_yes) - (1 - market_price_yes)  # = market_price_yes - fair_value_yes

    if abs(edge_yes) < config.MIN_EDGE_THRESHOLD:
        return BetDecision(
            should_trade=False,
            side="",
            size_usd=0,
            edge=edge_yes,
            kelly_fraction=0,
            reason=(
                f"Edge {abs(edge_yes):.2%} below threshold {config.MIN_EDGE_THRESHOLD:.2%}"
            ),
        )

    if edge_yes > 0:
        # Buy YES: market underprices YES
        side = "YES"
        p = fair_value_yes
        market_price = market_price_yes
    else:
        # Buy NO: market underprices NO
        side = "NO"
        p = 1 - fair_value_yes
        market_price = 1 - market_price_yes

    edge = abs(edge_yes)
    q = 1 - p

    # ── Kelly Criterion ───────────────────────────────────────────────────
    # For a binary bet at price `market_price`, if you buy at that price:
    #   Win payout = (1 - market_price) / market_price  (net profit per dollar risked)
    #   b = payout ratio
    if market_price <= 0 or market_price >= 1:
        return BetDecision(
            should_trade=False,
            side=side,
            size_usd=0,
            edge=edge,
            kelly_fraction=0,
            reason=f"Invalid market price: {market_price}",
        )

    b = (1 - market_price) / market_price  # payout odds
    kelly_full = (p * b - q) / b

    if kelly_full <= 0:
        return BetDecision(
            should_trade=False,
            side=side,
            size_usd=0,
            edge=edge,
            kelly_fraction=0,
            reason=f"Kelly fraction negative ({kelly_full:.4f}), no edge after odds",
        )

    # Quarter-Kelly, scaled by confidence
    kelly_adjusted = kelly_full * config.KELLY_FRACTION * confidence

    # ── Position sizing ───────────────────────────────────────────────────
    size_usd = kelly_adjusted * bankroll

    # Cap at max bet fraction
    max_bet = bankroll * config.MAX_BET_FRACTION
    if size_usd > max_bet:
        size_usd = max_bet

    # Minimum bet of $1 to avoid dust
    if size_usd < 1.0:
        return BetDecision(
            should_trade=False,
            side=side,
            size_usd=0,
            edge=edge,
            kelly_fraction=kelly_adjusted,
            reason=f"Bet size ${size_usd:.2f} below $1 minimum",
        )

    return BetDecision(
        should_trade=True,
        side=side,
        size_usd=round(size_usd, 2),
        edge=edge,
        kelly_fraction=kelly_adjusted,
        reason=f"Buy {side} — edge {edge:.2%}, Kelly {kelly_adjusted:.4f}, size ${size_usd:.2f}",
        details={
            "fair_value_yes": fair_value_yes,
            "market_price_yes": market_price_yes,
            "confidence": confidence,
            "kelly_full": kelly_full,
            "kelly_adjusted": kelly_adjusted,
            "payout_odds": b,
            "bankroll": bankroll,
            "max_bet": max_bet,
        },
    )
