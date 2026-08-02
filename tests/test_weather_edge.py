"""Tests for the weather fair-value strategy's probability model."""

from datetime import date

import pytest

from settings import Settings
from strategies.weather_edge import (
    WeatherEdgeStrategy,
    _phi,
    resolution_date_from_ticker,
    series_of,
)


def _strategy() -> WeatherEdgeStrategy:
    return WeatherEdgeStrategy(
        settings=Settings(_env_file=None),
        client=None,
        fee_model=None,
        risk_mgr=None,
        storage=None,
    )


def test_phi_basic():
    assert abs(_phi(0.0) - 0.5) < 1e-9
    assert _phi(3.0) > 0.99
    assert _phi(-3.0) < 0.01


def test_ticker_date_parsing():
    assert resolution_date_from_ticker("KXHIGHNY-26AUG02-B34.5") == date(2026, 8, 2)
    assert resolution_date_from_ticker("KXHIGHCHI-26FEB11-T40") == date(2026, 2, 11)
    assert resolution_date_from_ticker("NODATE") is None
    assert series_of("KXHIGHNY-26AUG02-B34.5") == "KXHIGHNY"


def test_greater_market_probabilities():
    s = _strategy()
    mkt = {"strike_type": "greater", "floor_strike": 75}
    # Forecast well above the strike → near-certain YES
    p_hi = s.fair_prob_temp(mkt, mu=82.0, sigma=2.0, current_f=None,
                            is_resolution_day=False, local_hour=10)
    assert p_hi > 0.95
    # Forecast well below the strike → near-certain NO
    p_lo = s.fair_prob_temp({"strike_type": "greater", "floor_strike": 90},
                            mu=82.0, sigma=2.0, current_f=None,
                            is_resolution_day=False, local_hour=10)
    assert p_lo < 0.05


def test_between_bucket_peaks_at_forecast():
    s = _strategy()
    centered = {"strike_type": "between", "floor_strike": 79, "cap_strike": 81}
    off = {"strike_type": "between", "floor_strike": 85, "cap_strike": 87}
    p_centered = s.fair_prob_temp(centered, mu=80.0, sigma=2.0, current_f=None,
                                  is_resolution_day=False, local_hour=10)
    p_off = s.fair_prob_temp(off, mu=80.0, sigma=2.0, current_f=None,
                             is_resolution_day=False, local_hour=10)
    assert p_centered > p_off


def test_observation_lock_yes():
    s = _strategy()
    mkt = {"strike_type": "greater", "floor_strike": 75}
    # Current temp already above the floor on resolution day → locked YES
    p = s.fair_prob_temp(mkt, mu=70.0, sigma=2.0, current_f=76.0,
                         is_resolution_day=True, local_hour=14)
    assert p >= 0.985


def test_observation_lock_no():
    s = _strategy()
    mkt = {"strike_type": "less", "cap_strike": 70}
    # High already exceeded the cap → "less than 70" is dead
    p = s.fair_prob_temp(mkt, mu=68.0, sigma=2.0, current_f=73.0,
                         is_resolution_day=True, local_hour=14)
    assert p <= 0.01

    bucket = {"strike_type": "between", "floor_strike": 65, "cap_strike": 70}
    p2 = s.fair_prob_temp(bucket, mu=68.0, sigma=2.0, current_f=73.0,
                          is_resolution_day=True, local_hour=14)
    assert p2 <= 0.01


def test_fallback_text_parsing():
    s = _strategy()
    mkt = {"title": "Will the high in NYC be 80 degrees or above?",
           "ticker": "KXHIGHNY-26AUG02-B80"}
    p = s.fair_prob_temp(mkt, mu=85.0, sigma=2.0, current_f=None,
                         is_resolution_day=False, local_hour=10)
    assert p is not None and p > 0.9


def test_kalshi_fee_formula():
    from engine.fee_calculator import FeeModel
    assert FeeModel.kalshi_trading_fee_cents(50) == 2      # 1.75 → 2¢
    assert FeeModel.kalshi_trading_fee_cents(90) == 1      # 0.63 → 1¢
    assert FeeModel.kalshi_trading_fee_cents(50, count=10) == 18  # 17.5 → 18¢


def test_settlement_reconciliation():
    import asyncio

    from engine.risk_manager import RiskManager

    class SettledClient:
        def __init__(self, result):
            self._result = result

        async def get_market(self, ticker):
            return {"market": {"status": "settled", "result": self._result}}

    class NullStorage:
        async def log_trade(self, **kw):
            self.last = kw

    async def run(result, side, price, count):
        settings = Settings(_env_file=None)
        risk = RiskManager(settings)
        storage = NullStorage()
        s = WeatherEdgeStrategy(settings=settings, client=SettledClient(result),
                                fee_model=None, risk_mgr=risk, storage=storage)
        s._positions["T1"] = {"side": side, "price_cents": price,
                              "count": count, "fair": 0.9,
                              "entered_at": 0.0}
        risk.record_fill("T1", side, count, price)
        await s.check_settlements()
        return s, risk, storage

    # Losing YES position: bought 2 @ 60¢, settled NO →
    # lose $1.20 + fee ceil(7*2*0.6*0.4)=4¢ → -$1.24
    s, risk, storage = asyncio.run(run("no", "yes", 60, 2))
    assert "T1" not in s._positions
    assert risk.daily_loss == pytest.approx(1.24)
    assert risk.total_exposure() == pytest.approx(0.0)  # exposure released
    assert storage.last["reason"] == "settlement no"

    # Winning YES position: bought 2 @ 60¢, settled YES → +$0.80 - 4¢ fee
    s, risk, storage = asyncio.run(run("yes", "yes", 60, 2))
    assert "T1" not in s._positions
    assert risk.daily_loss == pytest.approx(0.0)  # win, no loss accrued
    assert storage.last["pnl"] == pytest.approx(0.76)


def test_restore_positions_skips_settled():
    import asyncio
    import time as _t

    from engine.risk_manager import RiskManager

    class FakeStorage:
        async def get_trades(self, limit=100):
            now = _t.time()
            return [  # newest-first, like the real query
                {"strategy": "weather_edge", "ticker": "A", "side": "yes",
                 "price_cents": 100, "count": 2, "ts": now,
                 "reason": "settlement yes"},
                {"strategy": "weather_edge", "ticker": "A", "side": "yes",
                 "price_cents": 60, "count": 2, "ts": now - 100,
                 "reason": "fair=0.90"},
                {"strategy": "weather_edge", "ticker": "B", "side": "no",
                 "price_cents": 40, "count": 1, "ts": now - 50,
                 "reason": "fair=0.20"},
                {"strategy": "event_reversion", "ticker": "C", "side": "yes",
                 "price_cents": 50, "count": 1, "ts": now, "reason": "zscore"},
            ]

    async def run():
        settings = Settings(_env_file=None)
        risk = RiskManager(settings)
        s = WeatherEdgeStrategy(settings=settings, client=None, fee_model=None,
                                risk_mgr=risk, storage=FakeStorage())
        await s.restore_positions()
        return s

    s = asyncio.run(run())
    # A was settled → not restored; B is open → restored; C is another strategy
    assert set(s._positions.keys()) == {"B"}
    assert s._positions["B"]["count"] == 1
