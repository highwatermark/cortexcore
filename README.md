# Momentum Agent v2

AI-native options flow trading system powered by the Anthropic Claude Agent SDK. Monitors institutional options flow via the Unusual Whales API, scores signals using deterministic rules, and executes trades through Alpaca with a multi-layered safety architecture. Trades **calls only, ASK-side only** — following institutional buyers with no directional interpretation needed.

## Architecture

```
Monitor Loop (30-180s adaptive polling)
    |
    v
Orchestrator (Claude Sonnet 4 - lead agent)
    |
    +-- Flow Scanner ------> Unusual Whales API (/option-trades/flow-alerts)
    |                           - newer_than high-watermark (only new alerts)
    |                           - Calls only, ASK-side only, opening positions
    |                           - min_premium, DTE, vol/OI filters
    |
    +-- Position Manager ---> SQLite DB (positions, P&L, Greeks)
    |
    +-- Risk Manager -------> Portfolio risk scoring (0-100)
    |
    +-- Executor -----------> Alpaca broker (limit orders, fill polling)
                                |
                            Safety Gate (14 hard checks, non-overridable)
```

The orchestrator delegates work to four specialized subagents. Each subagent has its own tools and prompt (see `config/prompts/`). All trade execution passes through a deterministic safety gate that cannot be overridden by the AI.

## Quick Start

### Prerequisites

- Python 3.11+
- API keys: Anthropic, Unusual Whales, Alpaca (paper or live)
- Optional: Telegram bot token for notifications

### Installation

```bash
cd /home/ubuntu/momentum-agent-v2
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Configuration

Copy `.env.example` to `.env` and fill in your API keys:

```env
ANTHROPIC_API_KEY=sk-ant-...
UW_API_KEY=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
TELEGRAM_BOT_TOKEN=...       # optional
TELEGRAM_ADMIN_ID=...        # optional
PAPER_TRADING=true
SHADOW_MODE=false
```

All configuration lives in `config/settings.py` as a single Pydantic `Settings` class. Environment variables and `.env` are validated at startup.

### Running

```bash
# Continuous monitoring loop (production)
python -m scripts.run

# Single scan cycle (testing)
python -m scripts.run --once

# Risk assessment only
python -m scripts.run --risk

# Kill switch
python -m scripts.run --kill      # halt all trading
python -m scripts.run --unkill    # resume
```

### Systemd (production)

```bash
sudo cp momentum-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now momentum-agent
journalctl -u momentum-agent -f
```

## Project Structure

```
config/
    settings.py              # Unified Pydantic config (single source of truth)
    prompts/                 # External markdown prompts for each agent
        orchestrator.md
        flow_scanner.md
        position_manager.md
        risk_manager.md
        executor.md
core/
    safety.py                # 14-point deterministic safety gate
    circuit_breaker.py       # Loss-based trading halts (daily/weekly/consecutive)
    killswitch.py            # File-based emergency halt
    health.py                # Dependency health checks (APIs, DB, disk)
    reconciler.py            # Alpaca <-> DB position sync
    logger.py                # Structlog with correlation IDs + JSON output
    utils.py                 # OCC symbol parsing, DTE calculation
data/
    models.py                # SQLAlchemy ORM + Pydantic schemas
services/
    unusual_whales.py        # UW flow-alerts client with newer_than watermark
    alpaca_broker.py         # Alpaca order submission + account queries
    telegram.py              # Telegram notification service
tools/
    flow_tools.py            # scan_flow, score_signal, save_signal, send_scan_report
    position_tools.py        # get_open_positions, check_exit_triggers
    risk_tools.py            # calculate_portfolio_risk, pre_trade_check
    execution_tools.py       # execute_entry, execute_exit, position sizing
agents/
    orchestrator.py          # Lead agent + AgentRunner (agentic loop)
    definitions.py           # Prompt loading from external files
monitor/
    loop.py                  # Main event loop with market hours + circuit breakers
analytics/
    performance.py           # Win rate, profit factor, Sharpe, max drawdown
bot/
    commands.py              # Telegram bot (13+ commands, auth-gated)
scripts/
    run.py                   # CLI entry point
tests/
    unit/                    # Unit tests
    e2e/                     # End-to-end tests (requires live APIs)
```

## Signal Scoring

Signals from Unusual Whales are scored on a 0-10 scale. Minimum to pass: **7**.

### Reward Indicators

| Indicator              | Points |
|------------------------|--------|
| Sweep order            | +2     |
| Floor trade            | +2     |
| Opening position       | +2     |
| Vol/OI >= 1.5          | +1     |
| Vol/OI >= 3.0          | +1     |
| Premium >= $250K       | +1     |
| Premium >= $500K       | +2     |
| Directional >= 75%     | +1     |
| Directional >= 90%     | +2     |
| Single-leg (no multi)  | +1     |
| Block trade (<10)      | +1     |

### Penalty Indicators

| Indicator              | Points |
|------------------------|--------|
| IV rank > 70%          | -3     |
| DTE < 6                | -2     |
| DTE 6-14               | -1     |
| Earnings within 7 days | -2     |
| Non-CALL option        | blocked|

### API-Level Filters (applied before scoring)

- `is_call`: true, `is_put`: false (calls only)
- `is_ask_side`: true (institutional buyers only)
- `all_opening`: true (opening positions only)
- `min_premium`: $100,000
- `min_volume_oi_ratio`: 1.5
- `min_dte`: 6 (no upper limit — LEAPs allowed)
- `issue_types`: Common Stock (excludes ETFs)
- `newer_than`: Unix timestamp of last scan (high-watermark)

## Safety Architecture

### Safety Gate (`core/safety.py`)

14 deterministic checks executed before **every** order submission. These cannot be overridden by the AI agent:

1. Excluded ticker blocklist
2. Max positions (3)
3. Max total exposure (25% of equity)
4. Max position value ($1,000)
5. Max executions per day (2)
6. Daily loss limit (5% of equity)
7. Weekly loss limit (10% of equity)
8. Consecutive loss limit (2)
9. IV rank cap (70%)
10. Minimum DTE (6)
11. Max bid-ask spread (15%)
12. Earnings blackout (2 days)
13. Market timing (15 min buffer at open/close)
14. All blocks logged with `safety_gate_blocked` event

### Circuit Breakers (`core/circuit_breaker.py`)

Three independent loss-based breakers:

- **Daily**: Realized losses >= 5% equity -> halt rest of day
- **Weekly**: Realized losses >= 10% equity -> halt until Monday
- **Consecutive**: 2 consecutive losing trades -> halt 120 minutes

### Kill Switch (`core/killswitch.py`)

File-based emergency halt. If the `KILLSWITCH` file exists in the project root, all trading stops immediately. Controlled via CLI or Telegram `/killswitch` command.

## Exit Strategy

Exits are evaluated every monitoring cycle:

| Trigger                | Condition              | Priority |
|------------------------|------------------------|----------|
| DTE mandatory          | DTE <= 5               | Highest  |
| Hard stop loss         | P&L <= -35%            | High     |
| Adaptive profit target | P&L >= 15-40% by DTE   | High     |
| Gamma risk             | DTE <= 5 and P&L < 20% | Medium   |
| Conviction drop        | Conviction < 50%       | Medium   |

Adaptive profit targets by DTE:

| DTE Range | Target |
|-----------|--------|
| > 14      | 40%    |
| 7-14      | 35%    |
| 3-7       | 25%    |
| < 3       | 15%    |

## Unusual Whales API Integration

Uses the `/option-trades/flow-alerts` endpoint with a **high-watermark** pattern:

1. First call after startup: fetch latest alerts (no time filter)
2. Record `time.time()` as `_last_scan_ts`
3. Subsequent calls: pass `newer_than=<_last_scan_ts>` so the API only returns alerts created after the last poll
4. Client-side dedup set in `flow_tools.py` provides a second layer of protection against rescoring

This prevents the same contract from being scored repeatedly across poll cycles.

## Excluded Tickers

Blocked from trading (hedging noise, low signal-to-noise):

- **Index ETFs**: SPY, QQQ, IWM, DIA
- **Sector ETFs**: XLF, XLE, XLK, XLV, XLI, XLU, XLB, XLC, XLY, XLP, XLRE
- **Commodities/Bonds/Vol**: GLD, SLV, TLT, HYG, EEM, EFA, UNG, VXX, UVXY, SVXY
- **Leveraged**: SQQQ, TQQQ, SPXU, SPXL, UPRO
- **Meme/Low-quality**: AMC, GME, BBBY, MULN, HYMC, MMAT, ATER, DWAC, WISH, PLTR
- **Index options**: SPXW, SPX, NDX, XSP

## Monitoring & Observability

### Structured Logging

Three output streams via structlog:

- **Console**: Human-readable (journalctl compatible)
- **File**: `logs/momentum.log`
- **JSON**: `logs/momentum.jsonl` (machine-parseable)

Every event is tagged with `session_id` and `cycle_id` for tracing.

### Health Checks

Run every 30 minutes, alert on 3+ consecutive failures:

- Alpaca API connectivity
- Unusual Whales API connectivity
- Database accessibility
- Disk space (> 500MB)

### Telegram Bot

13+ interactive commands (auth-gated to admin only):

```
/health        - Run health checks
/status        - Mode, uptime, scan count
/positions     - Open positions with P&L
/orders        - Pending broker orders
/risk          - Portfolio risk score
/performance   - 30-day metrics (win rate, Sharpe, etc.)
/weekly        - 7-day report
/history       - Last 10 trades
/flow          - Manual scan trigger
/close <id>    - Close position by ID
/reconcile     - Sync positions with broker
/killswitch    - Toggle kill switch
/errors        - Last 10 error log entries
```

## Database

SQLite with SQLAlchemy ORM. Tables:

- **signals**: Scored flow signals (accepted/rejected)
- **positions**: Open/closed positions with Greeks and P&L
- **order_intents**: Idempotency layer (prevents duplicate orders on restart)
- **broker_orders**: Actual Alpaca order tracking
- **trade_log**: Completed trade P&L ledger (used by analytics and circuit breakers)

## Analytics

Pure SQL aggregates on the trade_log table:

- Win rate, profit factor, max drawdown
- Sharpe ratio (annualized, 5% risk-free rate)
- Average hold duration
- Daily P&L series

Available via `analytics/performance.py` and the Telegram `/performance` command.

## Development

```bash
# Run tests
pytest tests/ -v

# Lint
ruff check .

# Type check
mypy .
```

## Dependencies

| Package            | Purpose                    |
|--------------------|----------------------------|
| anthropic          | Claude API (agent SDK)     |
| alpaca-py          | Broker (orders, account)   |
| pydantic-settings  | Validated configuration    |
| sqlalchemy         | ORM + database             |
| httpx              | Async HTTP client          |
| structlog          | Structured logging         |
| pytz               | Timezone handling          |
| aiohttp            | Telegram bot long-polling  |
