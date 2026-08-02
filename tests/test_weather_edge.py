"""Tests for the weather fair-value strategy's probability model."""

from datetime import date

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
