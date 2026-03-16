# momentum-agent-v2 — Paper Trading Deployment

Deploy the autoresearch-optimized config to Alpaca paper trading for live validation.

## Context

Two autoresearch runs (89 total experiments) optimized the options flow trading strategy. The promoted config (commit `40b62d1`) achieved test_sharpe=8.60 on backtested data. However, the backtest Sharpe is likely inflated by a short favorable window. The goal of this paper trading deployment is to **VALIDATE whether the edge is real, not to make money**. We expect live Sharpe to be 2-3, not 8.6.

Key backtest findings:
- Liquidity filters (MIN_OI=100, MIN_VOL=50) doubled the Sharpe — filtering phantom liquidity
- Ticker exclusions (TSLA, MSTR, SMH, SNDK, GOOG, NVDA) removed proven losers
- Max 1 position per ticker forced diversification and eliminated correlated drawdowns
- Floor trades are noise (weight zeroed out)
- Wider stop loss (40% vs 25%) lets winners recover from temporary dips
- Slippage death point is ~3% — strategy requires fills within 2% of mid-price
- Strategy is calls-only and has never been tested in a bear market

---

## Step 1: Read the codebase

Read the full momentum-agent-v2 codebase. Understand:
- How signals are ingested from Unusual Whales
- How scoring and filtering works
- How orders are placed via Alpaca
- How positions are tracked and exits are managed
- Where config/parameters are set
- The current Alpaca paper trading setup

---

## Step 2: Apply the promoted config

Update the agent's configuration to match the autoresearch-optimized parameters:

### Scoring weights

```python
SWEEP_WEIGHT = 2
FLOOR_WEIGHT = 0                    # CHANGED: was 2, floor trades are noise
OPENING_WEIGHT = 2
VOL_OI_TIER1_WEIGHT = 1
VOL_OI_TIER2_WEIGHT = 1
PREMIUM_TIER1_WEIGHT = 2
PREMIUM_TIER2_WEIGHT = 4
DIRECTION_TIER1_WEIGHT = 1
DIRECTION_TIER2_WEIGHT = 2
SINGLELEG_WEIGHT = 1
BLOCK_TRADE_WEIGHT = 1
IV_RANK_PENALTY = -3
DTE_TIER1_PENALTY = -1
DTE_TIER2_PENALTY = -2
EARNINGS_PENALTY = -2
MIN_SCORE = 5
```

### Scoring thresholds

```python
IV_RANK_THRESHOLD = 70
VOL_OI_TIER1_THRESHOLD = 1.5
VOL_OI_TIER2_THRESHOLD = 3.0
PREMIUM_TIER1_THRESHOLD = 250000
PREMIUM_TIER2_THRESHOLD = 500000
DIRECTION_TIER1_THRESHOLD = 0.75
DIRECTION_TIER2_THRESHOLD = 0.90
DTE_TIER1_THRESHOLD = 14
DTE_TIER2_THRESHOLD = 6
```

### Position management

```python
MAX_POSITIONS = 20
MAX_EXECUTIONS_PER_DAY = 10
MAX_POSITION_VALUE = 1000           # Conservative starting size
MAX_TICKER_POSITIONS = 1            # CRITICAL: one position per ticker max
```

### Exit parameters

```python
STOP_LOSS_PCT = 0.40                # CHANGED: was 0.25, wider stop
MAX_HOLD_DTE = 5                    # CHANGED: was 3
PROFIT_TARGET_DTE_GT_14 = 0.50
PROFIT_TARGET_DTE_7_14 = 0.40
PROFIT_TARGET_DTE_3_7 = 0.30
PROFIT_TARGET_DTE_LT_3 = 0.20
```

### Liquidity filters

```python
MIN_OPEN_INTEREST = 100
MIN_VOLUME = 50
```

### Ticker exclusions

Score penalty of -100 effectively excludes these tickers:

```python
TICKER_WEIGHTS = {
    "TSLA": -100,
    "MSTR": -100,
    "SMH": -100,
    "SNDK": -100,
    "GOOG": -100,
    "NVDA": -100,
}
```

### Greek limits

```python
MAX_PORTFOLIO_DELTA = 5.5
```

---

## Step 3: Add fill quality logging

This is the **MOST IMPORTANT** addition. The backtest assumed 1% slippage. If live fills are worse than 2.5%, the strategy is not viable.

For every trade entry and exit, log to a file `fill_quality.jsonl`:

```json
{
    "timestamp": "2025-03-16T10:30:15Z",
    "action": "entry",
    "ticker": "AAPL",
    "signal_time": "2025-03-16T10:29:45Z",
    "signal_price_mid": 3.45,
    "fill_price": 3.52,
    "slippage_pct": 0.0203,
    "signal_to_fill_seconds": 30,
    "bid_at_fill": 3.40,
    "ask_at_fill": 3.55,
    "spread_pct": 0.0435,
    "option_symbol": "AAPL250321C00175000",
    "open_interest": 1250,
    "volume_at_signal": 340,
    "score": 7,
    "order_id": "...",
    "fill_quantity": 1
}
```

Log the same structure for exits. This data is what tells us whether the strategy survives real execution.

---

## Step 4: Add live circuit breakers

The backtest circuit breakers never fired because the strategy was too well-behaved in historical data. Live markets have fat tails. Implement these as **HARD STOPS** in the agent, not optional params:

```python
# Circuit breakers — these override all other logic
DAILY_LOSS_LIMIT_PCT = 0.05         # If realized + unrealized daily loss > 5% of account, close ALL positions and halt until next market open
CONSECUTIVE_LOSS_HALT = 5           # After 5 consecutive losing trades, pause new entries for 24 hours (keep managing exits on open positions)
WEEKLY_DRAWDOWN_HALT_PCT = 0.10     # If account is down > 10% from weekly high, halt ALL trading for 48 hours
MAX_SINGLE_TRADE_LOSS_PCT = 0.50    # Emergency stop: if any single position is down > 50%, exit immediately regardless of other exit logic (gap protection)
```

### When a circuit breaker fires:

1. Log the event to `circuit_breaker.log` with timestamp, which breaker, account state
2. Send a Telegram notification (use the existing Telegram bot) with: which breaker fired, current account value, positions still open
3. Continue managing exits on open positions (don't abandon them)
4. Do NOT enter any new positions until the halt period expires

---

## Step 5: Add daily performance summary

At market close each day (or when the agent's daily cycle completes), append to `daily_summary.jsonl`:

```json
{
    "date": "2025-03-16",
    "trades_entered": 2,
    "trades_exited": 1,
    "open_positions": 3,
    "realized_pnl": 145.30,
    "unrealized_pnl": -52.10,
    "total_pnl": 93.20,
    "account_value": 50093.20,
    "win_rate_running": 0.58,
    "avg_slippage_pct": 0.018,
    "worst_slippage_pct": 0.032,
    "portfolio_delta": 3.2,
    "signals_received": 45,
    "signals_passed_score": 12,
    "signals_passed_filters": 8,
    "signals_executed": 2,
    "circuit_breakers_fired": [],
    "tickers_traded": ["AAPL", "AMZN"]
}
```

Send this as a Telegram summary at end of day.

---

## Step 6: Add go/no-go evaluation logic

After 20 completed trades (entries that have been exited), compute and log:

```
=== 20-TRADE CHECKPOINT ===
Live win rate:        X.XX (target: > 0.45)
Live avg slippage:    X.XX% (target: < 2.5%)
Live Sharpe estimate: X.XX (target: > 1.0)
Worst single loss:    X.XX%
Max consecutive loss: N
Signal-to-fill avg:   N seconds

GO/NO-GO: [PASS/FAIL — list which criteria failed]
```

Send this via Telegram. If `win_rate < 0.35` OR `avg_slippage > 0.03` OR `Sharpe < 0.5`, the message should say:

> **RECOMMENDATION:** Strategy underperforming backtest expectations. Consider halting and investigating before continuing.

Do NOT auto-halt on the 20-trade checkpoint — just alert. The human decides.

Repeat the checkpoint at 40 trades and 60 trades.

### 60-trade final assessment

At 60 trades, compute a full assessment:

```
=== 60-TRADE FINAL ASSESSMENT ===
Live win rate:          X.XX (backtest: 0.644)
Live profit factor:     X.XX (backtest: 5.03)
Live Sharpe estimate:   X.XX (backtest: 8.60, expected: 2-3)
Live avg slippage:      X.XX%
Live max drawdown:      X.XX% (backtest: 1.1%)
Live expectancy:        $X.XX/trade (backtest: $285)
Avg signal-to-fill:     N seconds
Circuit breakers fired: N times

VERDICT: [READY FOR LIVE / NEEDS INVESTIGATION / NOT VIABLE]

READY FOR LIVE criteria (ALL must pass):
  - Win rate > 0.45
  - Sharpe > 1.5
  - Avg slippage < 2.5%
  - Max drawdown < 10%
  - Expectancy > $50/trade
  - No circuit breaker fired more than twice
```

---

## Step 7: Verify on paper account

After all code changes:

1. **Confirm paper trading API.** Check `APCA-API-BASE-URL` is `https://paper-api.alpaca.markets`, NOT `https://api.alpaca.markets`. If the codebase switches via environment variable, confirm env is set to paper.
2. **Dry-run first.** Run the agent in dry-run mode — process signals, compute scores, log what WOULD be traded, but don't place orders. Verify scoring and filtering match expectations from the backtest.
3. **Enable paper trading.** Confirm the first order places successfully on the paper account.
4. **Verify logging.** Confirm `fill_quality.jsonl` is being written correctly after the first trade.
5. **Verify Telegram.** Send a test notification to confirm alerts work.

---

## What NOT to do

- Do NOT modify the Unusual Whales data ingestion — the signal source stays the same
- Do NOT enable real money trading — paper only for the entire 60-day validation period
- Do NOT change the scoring logic beyond applying the config above — the backtest validated these specific parameters
- Do NOT add any new features or "improvements" — this deployment is for VALIDATION, not further development
- Do NOT increase position size above $1,000 during paper testing
- Do NOT remove ticker exclusions because "they seem to be working now" — re-validation requires a separate autoresearch run on fresh data
- Do NOT override circuit breakers for any reason

---

## Expected behavior

Based on backtest data, during paper trading you should see approximately:
- ~2 trades per day (10 max executions, filtered by score and liquidity)
- ~60% of signals filtered out by liquidity (OI < 100 or volume < 50)
- Win rate between 45-65% (backtest: 64.4%, expect regression to ~50-55%)
- Average hold time of 3-5 days
- Portfolio delta hovering between 2-5 (capped at 5.5)
- Max 1 position per ticker, so 5-10 different tickers in the portfolio at any time
- Total PnL of $50-150/day at $1,000 sizing (not $285/trade — that's the backtest number)

If you see fewer than 1 trade per day consistently, the liquidity filters may be too aggressive for current market conditions. If you see more than 5 trades per day, something is wrong with the MAX_EXECUTIONS_PER_DAY limit.

---

## Timeline

| Period | Action | Decision point |
|--------|--------|----------------|
| Day 1 | Deploy, dry-run, verify logging | First paper order places correctly |
| Week 1 | Observe, ~10 trades | Check slippage is < 2.5% |
| 20 trades (~Day 10) | First checkpoint | GO/NO-GO alert via Telegram |
| 40 trades (~Day 20) | Second checkpoint | Compare to 20-trade metrics |
| 60 trades (~Day 30) | Final assessment | READY FOR LIVE / NOT VIABLE |
| Day 30-60 | Continue if passing | Build confidence, monitor stability |
| Day 60 | Human decision | Scale to live at $1,000 or abort |
