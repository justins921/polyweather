"""Tests for inventory skew behavior in the market maker."""

import pytest

from strategies.market_maker import MarketMakerStrategy
from settings import Settings
from engine.fee_calculator import FeeModel
from engine.risk_manager import RiskManager


def _make_strategy(skew_per_contract: float = 1.0):
    settings = Settings(mm_skew_per_contract=skew_per_contract)
    risk = RiskManager(settings)
    fee = FeeModel()
    # We only need the pricing logic, so pass None for client/storage
    mm = MarketMakerStrategy(
        settings=settings,
        client=None,
        fee_model=fee,
        risk_mgr=risk,
        storage=None,
    )
    return mm


class TestSkewedPrices:
    def test_no_inventory_symmetric(self):
        mm = _make_strategy()
        bid, ask = mm._compute_skewed_prices(40, 50, net_contracts=0)
        # With no inventory, quotes should be roughly symmetric around mid=45
        mid = (bid + ask) / 2.0
        assert abs(mid - 45) <= 1

    def test_long_inventory_skews_down(self):
        mm = _make_strategy(skew_per_contract=1.0)
        bid_flat, ask_flat = mm._compute_skewed_prices(40, 50, net_contracts=0)
        bid_long, ask_long = mm._compute_skewed_prices(40, 50, net_contracts=3)
        # When long, we want to sell → lower ask to attract buyers
        # and widen bid to discourage more buying
        assert ask_long <= ask_flat
        assert bid_long <= bid_flat

    def test_short_inventory_skews_up(self):
        mm = _make_strategy(skew_per_contract=1.0)
        bid_flat, ask_flat = mm._compute_skewed_prices(40, 50, net_contracts=0)
        bid_short, ask_short = mm._compute_skewed_prices(40, 50, net_contracts=-3)
        # When short, we want to buy → raise bid to attract sellers
        # and widen ask to discourage more selling
        assert bid_short >= bid_flat
        assert ask_short >= ask_flat

    def test_skew_proportional_to_position(self):
        mm = _make_strategy(skew_per_contract=1.0)
        _, ask_1 = mm._compute_skewed_prices(40, 50, net_contracts=1)
        _, ask_3 = mm._compute_skewed_prices(40, 50, net_contracts=3)
        # Larger position → more skew
        assert ask_3 <= ask_1

    def test_quotes_never_cross(self):
        mm = _make_strategy(skew_per_contract=2.0)
        # Even with extreme inventory, bid < ask must hold
        for inv in range(-10, 11):
            bid, ask = mm._compute_skewed_prices(40, 50, net_contracts=inv)
            assert bid < ask, f"Crossed at inventory={inv}: bid={bid}, ask={ask}"
            assert 1 <= bid <= 98
            assert 2 <= ask <= 99

    def test_narrow_spread_doesnt_crash(self):
        mm = _make_strategy()
        bid, ask = mm._compute_skewed_prices(49, 51, net_contracts=0)
        assert bid < ask

    def test_zero_skew_is_symmetric(self):
        mm = _make_strategy(skew_per_contract=0.0)
        bid_0, ask_0 = mm._compute_skewed_prices(40, 50, net_contracts=0)
        bid_5, ask_5 = mm._compute_skewed_prices(40, 50, net_contracts=5)
        # With 0 skew, inventory doesn't affect quotes
        assert bid_0 == bid_5
        assert ask_0 == ask_5
