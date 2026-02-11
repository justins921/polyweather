"""Tests for risk manager limits enforcement."""

import time

import pytest

from engine.risk_manager import RiskManager
from settings import Settings


@pytest.fixture
def settings():
    return Settings(
        max_daily_loss=3.0,
        max_total_exposure=20.0,
        max_concurrent_markets=6,
        max_per_market_notional=5.0,
        max_per_order_notional=2.0,
        kill_switch_error_pct=5.0,
        kill_switch_window_secs=300,
        circuit_breaker_adverse_ticks=3,
        circuit_breaker_window_secs=10,
        circuit_breaker_cooldown_secs=300,
        allow_sports=True,
        allow_non_sports=True,
    )


@pytest.fixture
def risk_mgr(settings):
    return RiskManager(settings)


class TestDailyLoss:
    def test_no_loss_ok(self, risk_mgr):
        assert not risk_mgr.daily_loss_exceeded()

    def test_loss_accumulates(self, risk_mgr):
        risk_mgr.record_pnl(-1.5)
        risk_mgr.record_pnl(-1.0)
        assert risk_mgr.daily_loss == pytest.approx(2.5)
        assert not risk_mgr.daily_loss_exceeded()

    def test_loss_exceeded(self, risk_mgr):
        risk_mgr.record_pnl(-3.0)
        assert risk_mgr.daily_loss_exceeded()

    def test_profit_doesnt_reduce_loss(self, risk_mgr):
        risk_mgr.record_pnl(-2.0)
        risk_mgr.record_pnl(1.0)  # profit
        assert risk_mgr.daily_loss == pytest.approx(2.0)

    def test_blocks_order_after_loss(self, risk_mgr):
        risk_mgr.record_pnl(-3.0)
        ok, reason = risk_mgr.can_place_order("TEST-TICKER", 1.0)
        assert not ok
        assert "Daily loss" in reason


class TestExposureLimits:
    def test_per_order_limit(self, risk_mgr):
        ok, reason = risk_mgr.can_place_order("T1", 2.5)
        assert not ok
        assert "Order notional" in reason

    def test_per_market_limit(self, risk_mgr):
        risk_mgr.record_fill("T1", "yes", 10, 40)
        ok, reason = risk_mgr.can_place_order("T1", 1.5)
        assert not ok
        assert "Market exposure" in reason

    def test_total_exposure_limit(self, risk_mgr):
        for i in range(5):
            risk_mgr.record_fill(f"T{i}", "yes", 10, 40)  # 4.0 each = 20.0 total
        ok, reason = risk_mgr.can_place_order("T5", 1.0)
        assert not ok
        assert "Total exposure" in reason

    def test_concurrent_markets_limit(self, risk_mgr, settings):
        settings.max_concurrent_markets = 3
        rm = RiskManager(settings)
        for i in range(3):
            rm.record_fill(f"T{i}", "yes", 1, 50)
        ok, reason = rm.can_place_order("T_NEW", 0.5)
        assert not ok
        assert "Concurrent markets" in reason

    def test_existing_market_doesnt_count_as_new(self, risk_mgr, settings):
        settings.max_concurrent_markets = 2
        rm = RiskManager(settings)
        rm.record_fill("T1", "yes", 1, 50)
        rm.record_fill("T2", "yes", 1, 50)
        # Adding to existing market T1 should be OK
        ok, reason = rm.can_place_order("T1", 0.5)
        assert ok


class TestKillSwitch:
    def test_not_triggered_with_few_errors(self, risk_mgr):
        for _ in range(5):
            risk_mgr.record_api_error()
        assert not risk_mgr.is_killed()

    def test_triggered_when_error_rate_high(self, risk_mgr):
        # Need 10+ calls with >5% errors
        for _ in range(9):
            risk_mgr.record_api_success()
        # Now error rate is 0/9
        for _ in range(5):
            risk_mgr.record_api_error()
        # 5/14 = 35.7% > 5%
        if risk_mgr.is_killed():
            ok, reason = risk_mgr.can_place_order("T1", 0.5)
            assert not ok
            assert "Kill switch" in reason


class TestCategoryAllowlist:
    def test_sports_blocked_when_disabled(self, settings):
        settings.allow_sports = False
        rm = RiskManager(settings)
        ok, reason = rm.can_place_order("GAME1", 1.0, category="sports")
        assert not ok
        assert "Sports" in reason

    def test_non_sports_blocked_when_disabled(self, settings):
        settings.allow_non_sports = False
        rm = RiskManager(settings)
        ok, reason = rm.can_place_order("WEATHER1", 1.0, category="weather")
        assert not ok
        assert "Non-sports" in reason

    def test_disabled_category_blocks(self, risk_mgr):
        risk_mgr.disable_category("sports")
        ok, reason = risk_mgr.can_place_order("GAME1", 1.0, category="sports")
        assert not ok
        assert "disabled" in reason

    def test_re_enable_category(self, risk_mgr):
        risk_mgr.disable_category("sports")
        risk_mgr.enable_category("sports")
        ok, _ = risk_mgr.can_place_order("GAME1", 1.0, category="sports")
        assert ok


class TestCircuitBreaker:
    def test_adverse_selection_triggers_breaker(self, risk_mgr):
        risk_mgr.record_fill("T1", "yes", 1, 50, mid_cents=50)
        # Mid moved 5 ticks against us (we bought yes, mid dropped)
        triggered = risk_mgr.check_adverse_selection("T1", 45)
        assert triggered
        # Should block new orders
        ok, reason = risk_mgr.can_place_order("T1", 0.5)
        assert not ok
        assert "Circuit breaker" in reason

    def test_no_adverse_if_move_small(self, risk_mgr):
        risk_mgr.record_fill("T1", "yes", 1, 50, mid_cents=50)
        triggered = risk_mgr.check_adverse_selection("T1", 49)
        assert not triggered


class TestSummary:
    def test_summary_structure(self, risk_mgr):
        s = risk_mgr.summary()
        assert "daily_loss" in s
        assert "total_exposure" in s
        assert "active_markets" in s
        assert "killed" in s
        assert "circuit_breakers" in s
