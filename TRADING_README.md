# Kalshi Micro-Bankroll Trading Bot

Async-first trading bot for Kalshi prediction markets, designed for a **$100 starting bankroll** with a target of **$20-30/month net** after fees.

## Architecture

```
main.py                       ← async CLI entry point
├── clients/kalshi_svc.py     ← REST + WS client, RSA auth, rate limiter
├── engine/
│   ├── fee_calculator.py     ← FeeModel + net-edge profitability gate
│   ├── risk_manager.py       ← all risk limits, kill switch, circuit breakers
│   ├── market_filter.py      ← depth/spread/liquidity/category filters
│   ├── paper_engine.py       ← conservative paper trading sim
│   └── backtest_engine.py    ← CSV/JSONL replay backtester
├── strategies/
│   ├── market_maker.py       ← Strategy A: inventory-skewed micro MM
│   └── event_reversion.py    ← Strategy B: mean-reversion on event spikes
├── data/storage.py           ← aiosqlite persistence (trades, quotes, P&L)
├── settings.py               ← pydantic config (env + .env)
└── log_config.py             ← structured JSON logging
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env with your Kalshi API key and private key path

# 3. Run tests
python -m pytest tests/ -v
```

## Running

```bash
# Paper trading (recommended first — run for 14 days)
python main.py --paper --demo

# Live demo environment
python main.py --demo

# Live production (after paper validation)
python main.py

# Category control (Wisconsin — sports may be geofenced)
python main.py --allow-sports false
python main.py --allow-non-sports false

# Verbose logging
python main.py --paper --log-level DEBUG
```

## Risk Limits (Non-Negotiable)

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max daily loss | $3 | Survive > everything |
| Max total exposure | $20 | 20% of bankroll |
| Max concurrent markets | 6 | Diversification |
| Max per-market notional | $5 | No concentration |
| Max per-order notional | $2 | Start small |
| Kill switch | >5% API errors in 5min | Protect against failures |
| Circuit breaker | 3-tick adverse move in 10s | Adverse selection defense |

## Strategies

### Strategy A: Inventory-Skewed Market Making (Primary)

- Places passive limit orders near BBO
- **Net-edge gate**: every order must have positive expected value after fees + slippage
- **Inventory skew**: long position → widen bids, tighten asks (and vice versa)
- **Refresh**: quotes update every 10s, but only if price moved enough (avoids spam)
- **Adverse selection**: if filled and mid moves 3+ ticks against you in 10s → pull quotes, 5min cooldown

### Strategy B: Event Window Mean Reversion (Secondary)

- Tracks rolling price window per market
- Enters when price deviates >2σ from rolling mean
- Max $1 per trade, 1-2 contracts
- Hard stop-loss (5 ticks) + take-profit (3 ticks)
- Must pass same net-edge gate

## Fee Model

Every trade is gated by:

```
net_edge = gross_edge - entry_fee - exit_fee - slippage
if net_edge <= 0: DO NOT TRADE
```

Default fees: maker $0.01/contract, taker $0.03/contract, 2-tick slippage buffer.
All P&L is logged as gross, fees, and net separately.

## Paper Trading Validation

Before going live, run 14 days of paper trading:

```bash
python main.py --paper --demo
```

The paper engine:
- Adds 500ms simulated latency
- Fills only when price moves **through** your level (not touches)
- Applies full fee + slippage model
- Reports: gross/net P&L, win rate, max drawdown, per-market contribution

**Go-live criteria**: net-positive P&L, max drawdown < $5, no single market > 40% of profit.

## Daily Operations Checklist

1. Check logs: `tail -20 logs/bot.jsonl | python -m json.tool`
2. Verify daily P&L is within bounds
3. Confirm no kill-switch activations
4. Review circuit breaker triggers (may indicate market regime change)
5. Monitor API cost / rate limit headroom
6. Weekly: review per-market P&L distribution

## Stop Conditions

The bot automatically stops trading when:
- Daily loss hits $3
- Kill switch activates (API error rate >5% in rolling 5min)
- WebSocket disconnects 3+ times in 5min
- Bankroll drops below hard stop

Manual stop: `Ctrl+C` or `kill -TERM <pid>` — the bot cancels all resting orders before exiting.

## Configuration

All settings are configurable via `.env` or environment variables. See `.env.example` for the full list with defaults and descriptions.

Key tuning knobs:
- `MM_QUOTE_REFRESH_SECS` — quote update frequency (default 10s)
- `MM_SKEW_PER_CONTRACT` — how aggressively to skew with inventory (default 1.0)
- `SLIPPAGE_BUFFER_TICKS` — conservative slippage assumption (default 2)
- `ER_ENTRY_ZSCORE` — mean-reversion entry threshold (default 2.0)

## Database

Trades, quotes, and fills are stored in SQLite at `data/trading.db`. Query with:

```bash
sqlite3 data/trading.db "SELECT date(ts,'unixepoch'), SUM(net_pnl) FROM trades GROUP BY 1"
```
