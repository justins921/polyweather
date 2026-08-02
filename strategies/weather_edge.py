"""
Strategy C: Weather fair-value edge.

Connects the weather intelligence stack (NOAA/NWS forecasts, observation
locks, optional Claude probability estimates) to the trading engine.

For each Kalshi weather market:
1. Map the series ticker to a city and fetch the NOAA forecast (cached).
2. Compute a fair probability for the market's strike from the forecast
   high temperature, modelling forecast error as a normal distribution
   whose sigma grows with days-to-resolution.
3. Apply observation locks: on the resolution day the daily high can only
   go UP from the current reading, so some outcomes are already decided.
4. Buy YES (or NO) as a taker when fair value exceeds the market price by
   at least the configured edge threshold. Positions are held to
   settlement — that is where the P&L is realized.

Every order still passes through the RiskManager gates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import date, datetime
from typing import Any

import config
import weather as wx
from clients.kalshi_svc import KalshiClient, OrderRequest
from data.storage import Storage
from engine.fee_calculator import FeeModel
from engine.risk_manager import RiskManager
from settings import Settings

logger = logging.getLogger(__name__)

# Kalshi weather series → city key in config.CITY_COORDS
SERIES_CITY: dict[str, str] = {
    "KXHIGHNY": "new york",
    "KXHIGHCHI": "chicago",
    "KXHIGHLAX": "los angeles",
    "KXHIGHMIA": "miami",
    "KXHIGHMIAMI": "miami",
    "KXHIGHAUS": "austin",
    "KXHIGHDEN": "denver",
    "KXHIGHPHIL": "philadelphia",
    "KXRAINNYC": "new york",
    "KXRAINNY": "new york",
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _to_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (ValueError, TypeError):
        return None


def series_of(ticker: str) -> str:
    return ticker.split("-")[0].upper() if ticker else ""


def resolution_date_from_ticker(ticker: str) -> date | None:
    """Parse the measurement date embedded in a Kalshi weather ticker.

    e.g. KXHIGHNY-26AUG02-B34.5 → 2026-08-02
    """
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", ticker.upper())
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        return date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None


class WeatherEdgeStrategy:
    """Weather fair-value strategy: trade the gap between forecast-implied
    probability and market price. One entry per market, held to settlement."""

    def __init__(
        self,
        settings: Settings,
        client: KalshiClient,
        fee_model: FeeModel,
        risk_mgr: RiskManager,
        storage: Storage,
    ) -> None:
        self._s = settings
        self._client = client
        self._fee = fee_model
        self._risk = risk_mgr
        self._storage = storage

        # Weather cache: city → (fetch_ts, data)
        self._wx_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._wx_locks: dict[str, asyncio.Lock] = {}

        # Claude event analysis cache: event_ticker → (ts, {ticker: fair}, confidence)
        self._claude_cache: dict[str, tuple[float, dict[str, float], float]] = {}

        # One-shot entry tracking: ticker → entry info
        self._positions: dict[str, dict[str, Any]] = {}

        if settings.anthropic_api_key and not config.CLAUDE_API_KEY:
            config.CLAUDE_API_KEY = settings.anthropic_api_key

    # ── Restart safety ───────────────────────────────────────────────────

    async def restore_positions(self) -> None:
        """Rebuild in-memory positions from the trade log after a restart,
        so the auto-restarting launchd service can't double-enter markets."""
        try:
            rows = await self._storage.get_trades(limit=500)
        except Exception as exc:
            logger.warning("Weather: position restore failed: %s", exc)
            return

        cutoff = time.time() - 3 * 86400
        entries: dict[str, dict[str, Any]] = {}
        settled: set[str] = set()
        for r in rows:  # rows are newest-first
            if r.get("strategy") != "weather_edge" or (r.get("ts") or 0) < cutoff:
                continue
            t = r.get("ticker", "")
            if str(r.get("reason") or "").startswith("settlement"):
                settled.add(t)
            elif t not in entries:
                entries[t] = r

        for t, r in entries.items():
            if t in settled or t in self._positions:
                continue
            self._positions[t] = {
                "order_id": None,
                "side": r.get("side", "yes"),
                "price_cents": int(r.get("price_cents") or 0),
                "count": int(r.get("count") or 1),
                "fair": 0.0,
                "entered_at": r.get("ts") or time.time(),
            }
            self._risk.record_fill(
                t, r.get("side", "yes"), int(r.get("count") or 1),
                int(r.get("price_cents") or 0),
            )
            logger.info("Weather: restored open position %s %s x%s @ %s¢",
                        t, r.get("side"), r.get("count"), r.get("price_cents"))

    # ── Settlement reconciliation ────────────────────────────────────────

    async def check_settlements(self) -> None:
        """Poll held markets for settlement; realize P&L into the risk
        manager and release exposure. Throttled per position."""
        now = time.monotonic()
        for ticker in list(self._positions.keys()):
            pos = self._positions[ticker]
            # None = never checked → check immediately, then throttle.
            last = pos.get("last_check")
            if last is not None and now - last < self._s.weather_settle_check_secs:
                continue
            pos["last_check"] = now

            try:
                data = await self._client.get_market(ticker)
            except Exception as exc:
                logger.debug("Weather: settle check failed for %s: %s", ticker, exc)
                continue

            mkt = data.get("market", data)
            status = (mkt.get("status") or "").lower()
            result = (mkt.get("result") or "").lower()
            if status not in ("settled", "finalized") or result not in ("yes", "no"):
                continue

            price, count = pos["price_cents"], pos["count"]
            won = result == pos["side"]
            gross_c = (100 - price) * count if won else -price * count
            fee_c = FeeModel.kalshi_trading_fee_cents(price, count)
            net = (gross_c - fee_c) / 100.0

            self._risk.record_pnl(net)
            self._risk.close_position(ticker)
            del self._positions[ticker]

            logger.info(
                "Weather SETTLED: %s → %s  (%s %s x%d @ %d¢)  net=$%.2f",
                ticker, result.upper(), "WIN" if won else "LOSS",
                pos["side"], count, price, net,
                extra={"ticker": ticker, "action": "weather_settlement", "pnl": net},
            )
            await self._storage.log_trade(
                ticker=ticker,
                side=pos["side"],
                count=count,
                price_cents=100 if won else 0,
                strategy="weather_edge",
                reason=f"settlement {result}",
                pnl=net,
                fees=fee_c / 100.0,
                gross_pnl=gross_c / 100.0,
                net_pnl=net,
            )

    # ── Market selection ─────────────────────────────────────────────────

    def select_markets(self, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pick open weather markets we know how to model."""
        out = []
        for m in markets:
            if m.get("status") != "open":
                continue
            if series_of(m.get("ticker", "")) in SERIES_CITY:
                out.append(m)
        return out

    # ── Main entry point ─────────────────────────────────────────────────

    async def process_markets(self, markets: list[dict[str, Any]]) -> None:
        """Evaluate all weather markets, grouped by city so each NOAA
        forecast is fetched once per cycle."""
        if not self._s.weather_enabled or not markets:
            return

        by_city: dict[str, list[dict[str, Any]]] = {}
        for m in markets:
            city = SERIES_CITY.get(series_of(m.get("ticker", "")))
            if city:
                by_city.setdefault(city, []).append(m)

        tasks = [self._process_city(city, mkts) for city, mkts in by_city.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error("Weather strategy error: %s", r, exc_info=r)

    async def _process_city(self, city: str, markets: list[dict[str, Any]]) -> None:
        weather_data = await self._get_weather(city)
        if not weather_data:
            logger.info("Weather: no forecast data for %s — skipping %d markets",
                        city, len(markets))
            return

        claude_fairs = await self._claude_fair_values(markets, weather_data)

        for mkt in markets:
            try:
                await self._evaluate_market(city, mkt, weather_data, claude_fairs)
            except Exception as exc:
                logger.warning("Weather: evaluation failed for %s: %s",
                               mkt.get("ticker", "?"), exc)

    # ── Weather fetching (cached) ────────────────────────────────────────

    async def _get_weather(self, city: str) -> dict[str, Any] | None:
        lock = self._wx_locks.setdefault(city, asyncio.Lock())
        async with lock:
            cached = self._wx_cache.get(city)
            now = time.monotonic()
            if cached and now - cached[0] < self._s.weather_cache_ttl_secs:
                return cached[1]

            coords = config.CITY_COORDS.get(city)
            if not coords:
                return None
            lat, lon, cc = coords
            data = await asyncio.to_thread(
                wx.fetch_weather_for_city, city, lat, lon, cc
            )
            self._wx_cache[city] = (now, data)
            return data

    # ── Fair-value model ─────────────────────────────────────────────────

    def _forecast_high_f(
        self, weather_data: dict[str, Any], target: date, tz: Any
    ) -> float | None:
        """Extract the forecast daily-high (°F) for the target local date."""
        # 1) NWS grid maxTemperature (°C)
        grid = weather_data.get("grid_data") or {}
        for entry in grid.get("maxTemperature") or []:
            start = str(entry.get("validTime", "")).split("/")[0]
            try:
                dt = datetime.fromisoformat(start)
            except ValueError:
                continue
            local_date = dt.astimezone(tz).date() if dt.tzinfo else dt.date()
            if local_date == target and entry.get("value") is not None:
                return float(entry["value"]) * 9 / 5 + 32

        # 2) NWS forecast periods (°F, take max of the day's periods)
        best: float | None = None
        for p in weather_data.get("forecast") or []:
            st = p.get("startTime")
            temp = _to_float(p.get("temperature"))
            if not st or temp is None:
                continue
            try:
                dt = datetime.fromisoformat(st)
            except ValueError:
                continue
            local_date = dt.astimezone(tz).date() if dt.tzinfo else dt.date()
            if local_date == target and (p.get("temperatureUnit") or "F") == "F":
                best = temp if best is None else max(best, temp)
        if best is not None:
            return best

        # 3) Open-Meteo daily (°C)
        for d in weather_data.get("daily") or []:
            if d.get("date") == target.isoformat() and d.get("max_temp_c") is not None:
                return float(d["max_temp_c"]) * 9 / 5 + 32

        return None

    def fair_prob_temp(
        self,
        mkt: dict[str, Any],
        mu: float,
        sigma: float,
        current_f: float | None,
        is_resolution_day: bool,
        local_hour: int,
    ) -> float | None:
        """Probability that the market resolves YES, given forecast high `mu`
        (°F) and error stdev `sigma`. Applies observation locks."""
        strike_type = (mkt.get("strike_type") or "").lower()
        floor = _to_float(mkt.get("floor_strike"))
        cap = _to_float(mkt.get("cap_strike"))

        # Fallback: parse from question/ticker text
        if strike_type not in ("greater", "less", "between"):
            info = wx.parse_market_threshold(
                mkt.get("title", "") or mkt.get("yes_sub_title", ""),
                mkt.get("ticker", ""),
            )
            if not info or info.get("type") != "high_temp":
                return None
            if info.get("direction") == "above":
                strike_type, floor = "greater", float(info["threshold_f"])
            else:
                strike_type, cap = "less", float(info["threshold_f"])

        # On the resolution day the daily high is bounded below by the
        # current observation, and late in the day little upside remains.
        if is_resolution_day and current_f is not None:
            mu = max(mu, current_f)
            if local_hour >= 17:
                sigma = max(1.0, sigma * 0.5)

        if strike_type == "greater" and floor is not None:
            p = 1.0 - _phi((floor - mu) / sigma)
            if is_resolution_day and current_f is not None and current_f > floor:
                p = max(p, 0.985)  # locked YES: high already above floor
        elif strike_type == "less" and cap is not None:
            p = _phi((cap - mu) / sigma)
            if is_resolution_day and current_f is not None and current_f > cap:
                p = min(p, 0.01)   # locked NO: high already above cap
        elif strike_type == "between" and floor is not None and cap is not None:
            p = _phi((cap - mu) / sigma) - _phi((floor - mu) / sigma)
            if is_resolution_day and current_f is not None and current_f > cap:
                p = min(p, 0.01)   # locked NO: high already above the bucket
        else:
            return None

        return max(0.01, min(0.99, p))

    # ── Optional Claude blend ────────────────────────────────────────────

    async def _claude_fair_values(
        self, markets: list[dict[str, Any]], weather_data: dict[str, Any]
    ) -> dict[str, tuple[float, float]]:
        """If enabled, get Claude's fair values per ticker → (fair, confidence).
        Cached per event for weather_cache_ttl_secs. Returns {} when disabled
        or on any failure — the deterministic model stands alone."""
        if not (self._s.weather_use_claude and config.CLAUDE_API_KEY):
            return {}

        out: dict[str, tuple[float, float]] = {}
        by_event: dict[str, list[dict[str, Any]]] = {}
        for m in markets:
            ev = m.get("event_ticker") or series_of(m.get("ticker", ""))
            by_event.setdefault(ev, []).append(m)

        for ev, mkts in by_event.items():
            cached = self._claude_cache.get(ev)
            now = time.monotonic()
            if cached and now - cached[0] < self._s.weather_cache_ttl_secs:
                for t, f in cached[1].items():
                    out[t] = (f, cached[2])
                continue

            try:
                from analyzer import analyze_event

                event = {
                    "event_title": mkts[0].get("title", ev),
                    "end_date": mkts[0].get("close_time", ""),
                    "total_liquidity": sum(_to_float(m.get("liquidity")) or 0 for m in mkts),
                    "outcome_summary": [
                        {
                            "question": m.get("yes_sub_title") or m.get("title", ""),
                            "outcome_prices": [
                                round((_to_float(m.get("yes_ask")) or 50) / 100, 2)
                            ],
                        }
                        for m in mkts
                    ],
                }
                analysis = await asyncio.to_thread(analyze_event, event, weather_data, None)
                if analysis and len(analysis.buckets) == len(mkts):
                    fairs = {
                        m["ticker"]: float(b["fair_value_yes"])
                        for m, b in zip(mkts, analysis.buckets)
                    }
                    self._claude_cache[ev] = (now, fairs, analysis.confidence)
                    for t, f in fairs.items():
                        out[t] = (f, analysis.confidence)
                    logger.info("Weather/Claude: %s analyzed conf=%.2f (%s)",
                                ev, analysis.confidence, analysis.reasoning[:120])
            except Exception as exc:
                logger.warning("Weather/Claude analysis failed for %s: %s", ev, exc)

        return out

    # ── Per-market evaluation ────────────────────────────────────────────

    async def _evaluate_market(
        self,
        city: str,
        mkt: dict[str, Any],
        weather_data: dict[str, Any],
        claude_fairs: dict[str, tuple[float, float]],
    ) -> None:
        ticker = mkt.get("ticker", "")
        if not ticker or ticker in self._positions:
            return

        tz = wx.get_city_timezone(city)
        if tz is None:
            return
        now_local = datetime.now(tz)
        today = now_local.date()

        target = resolution_date_from_ticker(ticker)
        if target is None:
            close_iso = mkt.get("close_time", "")
            try:
                target = datetime.fromisoformat(
                    close_iso.replace("Z", "+00:00")
                ).astimezone(tz).date()
            except (ValueError, TypeError):
                return

        days_out = (target - today).days
        if days_out < 0 or days_out > self._s.weather_max_days_out:
            return

        is_rain = "RAIN" in series_of(ticker)
        current_c = wx._get_current_temp_c(weather_data)
        current_f = current_c * 9 / 5 + 32 if current_c is not None else None

        fair: float | None
        if is_rain:
            # Only trade rain on an observation lock (rain already falling).
            info = wx.parse_market_threshold(mkt.get("title", ""), ticker)
            check = wx.check_observation_vs_threshold(weather_data, info or {"type": "rain"})
            if not (check and check.get("outcome_known") and days_out == 0):
                return
            fair = 0.97 if check["outcome"] == "YES" else 0.03
            model_desc = f"rain_lock:{check['reason'][:60]}"
        else:
            mu = self._forecast_high_f(weather_data, target, tz)
            if mu is None:
                logger.debug("Weather: no forecast high for %s on %s", city, target)
                return
            sigma = (
                self._s.weather_forecast_sigma_base
                + self._s.weather_sigma_per_day * days_out
            )
            fair = self.fair_prob_temp(
                mkt, mu, sigma,
                current_f=current_f,
                is_resolution_day=(days_out == 0),
                local_hour=now_local.hour,
            )
            if fair is None:
                return
            model_desc = f"mu={mu:.1f}F sigma={sigma:.1f} obs={current_f or 'na'}"

        # Blend with Claude estimate when available (weight by confidence)
        if ticker in claude_fairs:
            c_fair, conf = claude_fairs[ticker]
            w = min(0.5, max(0.0, conf * 0.5))
            fair = (1 - w) * fair + w * c_fair
            model_desc += f" claude={c_fair:.2f}@{conf:.2f}"

        # ── Compare to market price ──────────────────────────────────────
        try:
            book = await self._client.get_orderbook(ticker)
        except Exception as exc:
            logger.debug("Weather: book fetch failed for %s: %s", ticker, exc)
            self._risk.record_api_error()
            return
        self._risk.record_api_success()

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])
        if not yes_bids or not no_bids:
            return
        best_bid = int(yes_bids[0][0])
        best_ask = 100 - int(no_bids[0][0])
        if not (0 < best_bid < best_ask < 100):
            return

        fair_c = fair * 100.0
        is_lock = fair >= 0.97 or fair <= 0.03
        required = (
            self._s.weather_lock_min_edge_cents if is_lock
            else self._s.weather_min_edge_cents
        )

        yes_edge = fair_c - best_ask          # buy YES at the ask
        no_edge = best_bid - fair_c           # buy NO at (100 - best_bid)

        if yes_edge >= required:
            side, price_cents, edge = "yes", best_ask, yes_edge
        elif no_edge >= required:
            side, price_cents, edge = "no", 100 - best_bid, no_edge
        else:
            logger.debug(
                "Weather %s: no edge (fair=%.0f bid=%d ask=%d need=%d) [%s]",
                ticker, fair_c, best_bid, best_ask, required, model_desc,
            )
            return

        # Conviction scaling: 1 contract at the edge threshold, +1 for each
        # additional multiple of it (locks hit max size fastest).
        max_count = max(1, min(self._s.weather_max_contracts, int(edge // required)))
        await self._enter(mkt, side, price_cents, fair, edge, model_desc, max_count)

    # ── Order placement ──────────────────────────────────────────────────

    async def _enter(
        self,
        mkt: dict[str, Any],
        side: str,
        price_cents: int,
        fair: float,
        edge: float,
        model_desc: str,
        max_count: int | None = None,
    ) -> None:
        ticker = mkt["ticker"]
        price_cents = max(1, min(99, price_cents))

        count = FeeModel.max_contracts(self._s.weather_max_notional, price_cents)
        count = max(1, min(count, max_count or self._s.weather_max_contracts))

        notional = count * price_cents / 100.0
        ok, reason = self._risk.can_place_order(
            ticker, notional, category=mkt.get("category", ""),
        )
        if not ok:
            logger.info("Weather: order blocked for %s: %s", ticker, reason)
            return

        try:
            order = OrderRequest(
                ticker=ticker,
                action="buy",
                side=side,
                count=count,
                yes_price=price_cents if side == "yes" else (100 - price_cents),
            )
            result = await self._client.place_order(order)
        except Exception as exc:
            logger.warning("Weather: entry order failed for %s: %s", ticker, exc)
            self._risk.record_api_error()
            return

        self._risk.record_api_success()
        self._positions[ticker] = {
            "order_id": result.get("order_id"),
            "side": side,
            "price_cents": price_cents,
            "count": count,
            "fair": fair,
            "entered_at": time.time(),
        }
        # Taker order at the touch — assume filled for exposure accounting.
        self._risk.record_fill(ticker, side, count, price_cents)

        logger.info(
            "Weather ENTRY: %s %s x%d @ %d¢  fair=%.0f¢ edge=%.1f¢  [%s]",
            side, ticker, count, price_cents, fair * 100, edge, model_desc,
            extra={"ticker": ticker, "side": side, "action": "weather_entry"},
        )
        await self._storage.log_trade(
            ticker=ticker,
            side=side,
            count=count,
            price_cents=price_cents,
            strategy="weather_edge",
            reason=f"fair={fair:.2f} edge={edge:.1f}c {model_desc}"[:200],
        )
