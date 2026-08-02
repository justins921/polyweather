"""Tests for fee calculator and net-edge gate."""

import pytest

from engine.fee_calculator import FeeModel


@pytest.fixture
def fee_model():
    return FeeModel(maker_fee=0.01, taker_fee=0.03, slippage_ticks=2)


class TestFeeModel:
    def test_spread_cents(self, fee_model):
        assert fee_model.spread_cents(40, 45) == 5

    def test_half_spread(self, fee_model):
        assert fee_model.half_spread_cents(40, 46) == 3.0

    def test_net_edge_maker_positive(self, fee_model):
        # Kalshi charges the winning side only at settlement:
        # spread=10, gross=5, expected fee=0.01*0.5*100=0.5,
        # maker slippage=max(2-1,0)=1, net=3.5
        net = fee_model.net_edge_cents(40, 50, is_maker=True)
        assert net == 3.5

    def test_net_edge_maker_negative(self, fee_model):
        # spread=2, gross=1, fees=2, slippage=2, net=-3
        net = fee_model.net_edge_cents(49, 51, is_maker=True)
        assert net < 0

    def test_net_edge_taker_more_expensive(self, fee_model):
        maker = fee_model.net_edge_cents(40, 50, is_maker=True)
        taker = fee_model.net_edge_cents(40, 50, is_maker=False)
        assert taker < maker

    def test_passes_gate_true(self, fee_model):
        assert fee_model.passes_gate(35, 50, is_maker=True)

    def test_passes_gate_false(self, fee_model):
        assert not fee_model.passes_gate(48, 50, is_maker=True)

    def test_zero_spread_fails_gate(self, fee_model):
        assert not fee_model.passes_gate(50, 50, is_maker=True)

    def test_entry_fee(self, fee_model):
        assert fee_model.entry_fee(10, is_maker=True) == pytest.approx(0.10)
        assert fee_model.entry_fee(10, is_maker=False) == pytest.approx(0.30)

    def test_round_trip_fee(self, fee_model):
        assert fee_model.round_trip_fee(5, is_maker=True) == pytest.approx(0.10)
        assert fee_model.round_trip_fee(5, is_maker=False) == pytest.approx(0.30)


class TestPnL:
    def test_winning_yes_trade(self, fee_model):
        pnl = fee_model.compute_pnl(
            entry_price_cents=40,
            exit_price_cents=55,
            count=2,
            side="yes",
        )
        assert pnl["gross_pnl"] == pytest.approx(0.30)  # 15c * 2 = 30c
        assert pnl["fees"] == pytest.approx(0.04)         # 0.01 * 2 * 2
        assert pnl["net_pnl"] == pytest.approx(0.26)

    def test_losing_yes_trade(self, fee_model):
        pnl = fee_model.compute_pnl(
            entry_price_cents=50,
            exit_price_cents=40,
            count=3,
            side="yes",
        )
        assert pnl["gross_pnl"] == pytest.approx(-0.30)
        assert pnl["net_pnl"] < pnl["gross_pnl"]  # fees make it worse

    def test_no_side_pnl(self, fee_model):
        pnl = fee_model.compute_pnl(
            entry_price_cents=60,
            exit_price_cents=50,
            count=1,
            side="no",
        )
        # NO side: profit when price drops
        assert pnl["gross_pnl"] == pytest.approx(0.10)


class TestSizing:
    def test_max_contracts_basic(self):
        # $2 notional at 50c = 4 contracts
        assert FeeModel.max_contracts(2.0, 50) == 4

    def test_max_contracts_truncates(self):
        # $1 at 30c = 3.33 → 3 contracts (truncate, never round up)
        assert FeeModel.max_contracts(1.0, 30) == 3

    def test_max_contracts_edge_prices(self):
        assert FeeModel.max_contracts(1.0, 0) == 0
        assert FeeModel.max_contracts(1.0, 100) == 0

    def test_max_contracts_penny(self):
        # $1 at 1c = 100 contracts
        assert FeeModel.max_contracts(1.0, 1) == 100
