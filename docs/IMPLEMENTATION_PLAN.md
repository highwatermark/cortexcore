# Momentum Agent v2 — Implementation Plan

**Created:** 2026-02-09
**Status:** In Progress
**Reference:** `/home/ubuntu/momentum-agent/docs/roadmap_v2.md`

This document tracks all gaps between the v2 codebase and the production roadmap.
Each item has a phase, priority, status, and test criteria.

---

## Parameter Reconciliation

Before any code changes, these are the authoritative values. Any deviation is a bug.

| Parameter | Current v2 | Roadmap | **Authoritative** | Action |
|-----------|-----------|---------|-------------------|--------|
| max_positions | 4 | 3 | **3** | Change |
| max_per_trade_pct | 0.02 (2%) | 0.10 | **0.20 (20%)** | Change |
| max_per_trade_abs | $2,000 | $500 | **$1,000** | Change |
| max_exposure_pct | N/A ($8K abs) | 30% | **25%** | Add field + enforce |
| profit_target_pct | 0.50 (50%) | 0.40 | **0.40 (40%)** | Change |
| stop_loss_pct | 0.50 (50%) | 0.35 | **0.35 (35%)** | Change |
| max_hold_dte | N/A | 5 | **5** (mandatory exit) | Add field + enforce |
| max_executions_per_day | N/A | 2 | **2** | Add field + enforce |
| cooldown_minutes (CB) | 300s (5 min) | 120 min | **120 min (7200s)** | Change |
| market_open_delay_min | N/A | 15 | **15** | Add field + enforce |
| market_close_buffer_min | N/A | 15 | **15** | Add field + enforce |
| poll_interval_sec | 45 | 90 | **90** | Change |
| max_consecutive_losses | 5 (errors) | 3 | **2** (trading losses) | Change concept + value |
| max_daily_loss_pct | N/A | 5% | **5%** | Add field + enforce |
| max_weekly_loss_pct | N/A | 10% | **10%** | Add field + enforce |
| earnings_blackout_days | 2 (config only) | 2 | **2** (enforce in code) | Add enforcement |
| max_spread_pct | 25% (config only) | 15% | **15%** | Change + enforce |
| daily_claude_budget | N/A | $15 | **Skip** (per user) | No action |

---

## Phase 1: Critical Safety (Must-Have Before Market Open)

### 1.1 Fix Parameter Values
- **Priority:** P0
- **Status:** [ ] Pending
- **Files:** `config/settings.py`
- **Changes:**
  - `max_positions`: 4 → 3
  - `max_position_value`: $2,000 → $1,000
  - Add `max_per_trade_pct`: 0.20
  - Add `max_total_exposure_pct`: 0.25
  - `profit_target_pct`: 0.50 → 0.40
  - `stop_loss_pct`: 0.50 → 0.35
  - Add `max_hold_dte`: 5
  - Add `max_executions_per_day`: 2
  - `circuit_breaker_cooldown_seconds`: 300 → 7200
  - Add `market_open_delay_minutes`: 15
  - Add `market_close_buffer_minutes`: 15
  - `poll_interval_seconds`: 45 → 90
  - `max_consecutive_errors`: 5 → keep (this is error-based, separate from loss-based)
  - Add `max_consecutive_losses`: 2
  - Add `max_daily_loss_pct`: 0.05
  - Add `max_weekly_loss_pct`: 0.10
  - `max_spread_pct`: 25% → 15%
- **Test:** Unit test that loads settings and asserts every authoritative value matches.

### 1.2 Hard Safety Gate (Deterministic Hook Layer)
- **Priority:** P0
- **Status:** [ ] Pending
- **Files:** NEW `core/safety.py`, modify `tools/execution_tools.py`
- **Design:**
  - Create `SafetyGate` class with a single method: `check_entry(signal, portfolio_state) -> (allowed: bool, reason: str)`
  - Checks are **deterministic, non-overridable** — no "exceptional conviction" bypass
  - Called inside `execute_entry()` BEFORE order submission, not as an agent tool
  - Checks enforced:
    1. `max_positions` not exceeded
    2. `max_total_exposure_pct` not exceeded
    3. `max_per_trade_abs` not exceeded
    4. `max_executions_per_day` not exceeded (query OrderIntent table for today's fills)
    5. `max_daily_loss_pct` not exceeded (query TradeLog for today's realized losses)
    6. `max_weekly_loss_pct` not exceeded (query TradeLog for this week's realized losses)
    7. `max_consecutive_losses` not exceeded (query TradeLog for recent consecutive losses)
    8. `max_iv_rank_for_entry` (70%) enforced
    9. `min_dte_for_entry` (6) enforced
    10. `max_spread_pct` (15%) enforced
    11. `earnings_blackout_days` (2) enforced — check via UW earnings endpoint or Alpaca
    12. `market_open_delay_minutes` (15) enforced — block entries in first 15 min
    13. `market_close_buffer_minutes` (15) enforced — block entries in last 15 min
    14. Ticker not in `EXCLUDED_TICKERS`
  - Every rejection logged with structured event: `safety_gate_blocked`
  - Every approval logged: `safety_gate_passed`
- **Test:** Unit tests for every gate condition (14+ tests). Test that no parameter combination bypasses the gate.

### 1.3 Trading Loss Circuit Breakers
- **Priority:** P0
- **Status:** [ ] Pending
- **Files:** NEW `core/circuit_breaker.py`, modify `monitor/loop.py`
- **Design:**
  - `TradingCircuitBreaker` class, checked each tick before scanning
  - Three independent breakers:
    1. **Daily loss:** If realized losses today exceed `max_daily_loss_pct` (5%) of account equity → pause trading for the rest of the day
    2. **Weekly loss:** If realized losses this week (Mon-Fri) exceed `max_weekly_loss_pct` (10%) → pause trading until Monday
    3. **Consecutive losses:** If last N closed trades are all losses where N = `max_consecutive_losses` (2) → pause for `cooldown_minutes` (120 min)
  - Each breaker has its own state: `is_tripped`, `tripped_at`, `resumes_at`, `reason`
  - Telegram alert on every trip and every reset
  - Breaker state survives process restart (persisted to DB or file)
- **Test:** Unit tests simulating loss sequences. Integration test: inject 2 consecutive losses, verify breaker trips and entries are blocked.

### 1.4 Kill Switch
- **Priority:** P0
- **Status:** [ ] Pending
- **Files:** Modify `monitor/loop.py`, add kill switch file mechanism
- **Design:**
  - File-based kill switch: if `/home/ubuntu/momentum-agent-v2/KILLSWITCH` file exists → all trading halted
  - Monitor loop checks at start of every tick
  - When tripped: cancel all pending orders, stop scanning, send Telegram alert
  - CLI helper: `python -m scripts.run --kill` creates the file, `--unkill` removes it
  - Later (Phase 5) wired to Telegram `/killswitch` command
  - Separate from circuit breakers — this is a manual emergency stop
- **Test:** Create killswitch file, verify loop stops trading. Remove file, verify loop resumes.

---

## Phase 2: Execution Safety

### 2.1 Market Open Delay Enforcement
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `core/safety.py` (gate #12), `monitor/loop.py`
- **Design:**
  - SafetyGate blocks entries if current time < market_open + 15 minutes
  - Monitor loop also skips full scan cycles during the delay window (position checks only)
- **Test:** Mock time to 9:35 ET, verify entry blocked. Mock time to 9:46 ET, verify entry allowed.

### 2.2 Market Close Buffer Enforcement
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `core/safety.py` (gate #13), `monitor/loop.py`
- **Design:**
  - SafetyGate blocks entries if current time > market_close - 15 minutes
  - Exits still allowed (closing positions is always permitted)
- **Test:** Mock time to 3:46 PM ET, verify entry blocked but exit allowed.

### 2.3 Max Executions Per Day Enforcement
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `core/safety.py` (gate #4)
- **Design:**
  - Count today's FILLED entry orders from OrderIntent table
  - If count >= `max_executions_per_day` (2), block new entries
  - Exits don't count toward the limit
- **Test:** Insert 2 filled entry intents for today, verify 3rd entry blocked.

### 2.4 Earnings Blackout Enforcement
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `core/safety.py` (gate #11), possibly `services/unusual_whales.py`
- **Design:**
  - Before entry, check if ticker has earnings within `earnings_blackout_days` (2)
  - Data source: UW API `/api/stock/{ticker}/earnings` or similar endpoint
  - Cache earnings dates per ticker per day (avoid repeated API calls)
  - If no earnings data available, log warning but allow trade (fail-open for data, not safety)
- **Test:** Mock earnings date 1 day away, verify entry blocked. Mock 5 days away, verify allowed.

### 2.5 Spread Check Enforcement
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `core/safety.py` (gate #10)
- **Design:**
  - `max_spread_pct` reduced from 25% → 15%
  - Requires bid/ask data at entry time
  - If bid/ask not available in signal data, skip check with warning log
- **Test:** Signal with 20% spread → blocked. Signal with 10% spread → allowed.

### 2.6 Adaptive Profit Targets (DTE-Based)
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `config/settings.py`, `tools/position_tools.py`
- **Design:**
  - Add to settings:
    ```
    adaptive_targets:
      dte_gt_14: 0.40
      dte_7_to_14: 0.35
      dte_3_to_7: 0.25
      dte_lt_3: 0.15
    ```
  - `check_exit_triggers()` uses DTE-appropriate target instead of fixed 40%
  - Existing `profit_target_pct` (0.40) becomes the default / max
- **Test:** Position with DTE 20, P&L +38% → no trigger. Same position with DTE 5, P&L +26% → trigger fires.

### 2.7 Max Hold DTE Mandatory Exit
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** `config/settings.py`, `tools/position_tools.py`
- **Design:**
  - Add `max_hold_dte: 5` to settings
  - New exit trigger `DTE_MANDATORY`: if `dte_remaining <= max_hold_dte` → exit regardless of P&L
  - Takes priority over all other triggers
- **Test:** Position with DTE 4 → mandatory exit trigger. DTE 6 → no trigger.

---

## Phase 3: Monitoring & Reconciliation

### 3.1 Full Position Reconciliation
- **Priority:** P1
- **Status:** [ ] Pending
- **Files:** NEW `core/reconciler.py`, modify `monitor/loop.py`
- **Design:**
  - Run every N cycles (e.g., every 5th cycle) during market hours
  - Fetch all positions from Alpaca broker
  - Fetch all OPEN positions from PositionRecord table
  - Compare:
    - **Orphans** (in Alpaca, not in DB): Create PositionRecord, alert operator via Telegram
    - **Phantoms** (in DB, not in Alpaca): Mark as CLOSED in DB, alert operator
    - **Price drift** (>10% discrepancy): Update DB, log warning
  - Log every reconciliation run with counts
- **Test:** Insert phantom position in DB (no matching Alpaca position), verify it gets flagged. Mock orphan in Alpaca response, verify it gets adopted.

### 3.2 Health Check System
- **Priority:** P2
- **Status:** [ ] Pending
- **Files:** NEW `core/health.py`, modify `monitor/loop.py`
- **Design:**
  - `HealthChecker` class with checks:
    1. **Alpaca API**: `GET /v2/account` succeeds
    2. **UW API**: screener endpoint returns data
    3. **Anthropic API**: ping with minimal message
    4. **DB**: session can be opened, tables exist
    5. **Disk space**: > 500MB free
    6. **Last scan age**: < 5 minutes during market hours
  - Run health check:
    - At startup
    - Every 30 minutes during market hours
    - On 3 consecutive failures → Telegram alert
  - Results stored for `/health` Telegram command (Phase 5)
- **Test:** Mock Alpaca API failure, verify health check reports unhealthy. Verify Telegram alert after 3 failures.

### 3.3 Correlation IDs in Logging
- **Priority:** P2
- **Status:** [ ] Pending
- **Files:** `core/logger.py`, `monitor/loop.py`, `agents/orchestrator.py`
- **Design:**
  - Generate `session_id` at startup (e.g., `sess-20260209-a1b2c3`)
  - Generate `cycle_id` per tick (e.g., `cyc-0042`)
  - Use structlog `contextvars` to bind these at the start of each cycle
  - All log events within a cycle automatically tagged
  - Signal-level `signal_id` already exists — flows through naturally
- **Test:** Run a cycle, verify all log entries contain session_id and cycle_id.

---

## Phase 4: Analytics & Performance Tracking

### 4.1 Performance Module
- **Priority:** P2
- **Status:** [ ] Pending
- **Files:** NEW `analytics/__init__.py`, NEW `analytics/performance.py`
- **Design:**
  - Functions (all query TradeLog table):
    - `get_win_rate(days=30) -> float` — % of trades closed at profit
    - `get_profit_factor(days=30) -> float` — sum(wins) / sum(losses)
    - `get_max_drawdown(days=30) -> float` — peak-to-trough equity decline %
    - `get_sharpe_ratio(days=30) -> float` — annualized, risk-free = 5%
    - `get_avg_hold_hours(days=30) -> float`
    - `get_signal_conversion_rate(days=30) -> float` — signals scored 7+ → executed
    - `get_pnl_by_signal_type(days=30) -> dict` — sweep vs floor vs open
    - `get_daily_pnl(days=30) -> list[dict]` — daily P&L series
    - `get_performance_summary(days=30) -> dict` — all of the above combined
  - No ML, no complexity — pure SQL aggregate queries on TradeLog
- **Test:** Insert 10 synthetic trades (6 wins, 4 losses), verify win_rate=60%, profit_factor correct, drawdown correct.

### 4.2 Enhanced Daily Summary
- **Priority:** P2
- **Status:** [ ] Pending
- **Files:** `monitor/loop.py`, `services/telegram.py`
- **Design:**
  - Expand `_send_daily_summary()` to include:
    - Account equity and day change
    - Rolling 30-day win rate and profit factor
    - Flow pipeline stats (scanned, scored 7+, executed)
    - Portfolio Greeks snapshot
    - Circuit breaker state
  - Format matches roadmap Section 8 EOD template
- **Test:** Insert test trades, trigger daily summary, verify Telegram message contains all fields.

### 4.3 Performance-Aware Agent Prompts
- **Priority:** P3
- **Status:** [ ] Pending
- **Files:** `agents/orchestrator.py`, `agents/definitions.py`
- **Design:**
  - At the start of each scan cycle, inject performance context into the orchestrator prompt:
    ```
    PERFORMANCE CONTEXT:
    - Win rate (30d): X%
    - Current drawdown: X%
    - Consecutive losses: N
    - Trades today: N / 2 max
    ```
  - Orchestrator adapts behavior based on performance (but hooks enforce limits regardless)
- **Test:** Verify prompt includes performance data when trades exist.

---

## Phase 5: Interactive Telegram Bot

### 5.1 Telegram Command Handler
- **Priority:** P3
- **Status:** [ ] Pending
- **Files:** NEW `bot/__init__.py`, NEW `bot/commands.py`, modify `services/telegram.py`
- **Design:**
  - Long-polling Telegram bot (runs as asyncio task within the monitor loop)
  - Commands:
    - `/health` — Run health checks, return status
    - `/status` — Mode, uptime, scan count, circuit breaker state
    - `/positions` — Open positions with P&L, DTE, Greeks
    - `/risk` — Portfolio risk score breakdown
    - `/performance` — 30-day metrics (win rate, Sharpe, drawdown)
    - `/killswitch on|off` — Create/remove killswitch file
    - `/shadow on|off` — Toggle shadow mode (requires restart to take effect, or runtime flag)
    - `/errors` — Last 10 error log entries
  - Auth: Only process messages from configured `TELEGRAM_CHAT_ID`
  - Rate limit: Max 1 command per 5 seconds
- **Test:** Send `/health` command, verify response. Send `/killswitch on`, verify file created. Send from unauthorized chat, verify ignored.

---

## Phase 6: Reliability & Operations

### 6.1 Retry/Backoff for All Services
- **Priority:** P2
- **Status:** [ ] Pending
- **Files:** `services/unusual_whales.py`, `services/telegram.py`
- **Design:**
  - UW API: 2 retries, exponential backoff (2s base)
  - Telegram: 3 retries, linear backoff (500ms base)
  - Claude API: Already implemented (3 retries, exponential)
  - Alpaca: No retry on order submission (idempotency risk), 3 retries on data reads
- **Test:** Mock UW API returning 500, verify retry + backoff. Verify order submission is NOT retried.

### 6.2 Structured JSON Logging
- **Priority:** P3
- **Status:** [ ] Pending
- **Files:** `core/logger.py`
- **Design:**
  - Add JSON renderer for file output (keep console as human-readable)
  - Log file: `logs/momentum.jsonl` (one JSON object per line)
  - Each entry includes: timestamp, level, event, logger, session_id, cycle_id, plus any bound context
  - Console output unchanged (human-readable for `journalctl`)
- **Test:** Verify log file contains valid JSON lines with all required fields.

### 6.3 Database Backup
- **Priority:** P3
- **Status:** [ ] Pending
- **Files:** NEW `scripts/backup.sh`
- **Design:**
  - Daily backup at 4:30 PM ET (cron job)
  - Copy `data/momentum.db` → `data/backups/momentum_YYYYMMDD.db`
  - Retain 30 days, delete older
  - Log backup result
  - Add to crontab (uncommented, active)
- **Test:** Run backup script manually, verify copy created. Verify old backups get cleaned.

### 6.4 Full Position Reconciliation (see 3.1)

---

## Phase 7: Future (Post-Stabilization)

These items are from the roadmap but deferred until the system is stable and has trade history.

### 7.1 Dynamic Equity-Tier Sizing
- **Priority:** P4
- **Status:** [x] Partially Done — deterministic position sizing implemented
- **Description:** Scale max_positions, max_per_trade_pct, max_exposure_pct based on account equity tiers ($5K, $10K, $25K, $50K). Not needed until account grows significantly.
- **What's done:** `calculate_position_size()` in `tools/execution_tools.py` computes max contracts from equity, per-trade %, position cap, and remaining exposure. Wired into `execute_entry()` to auto-cap quantity. Exposed as agent tool. 13 tests in `tests/unit/test_execution_tools.py`.
- **What's left:** Tier-based parameter scaling (adjust limits based on account size tiers).

### 7.2 DQN Learning Pipeline
- **Priority:** P4
- **Status:** [ ] Pending
- **Description:** Feature extraction, DQN model, nightly training, champion/challenger evaluation, advisory tool for orchestrator. Requires 100+ completed trades for meaningful training data.

### 7.3 Weekly Self-Review Agent
- **Priority:** P4
- **Status:** [ ] Pending
- **Description:** Subagent running Sunday 6 PM ET that analyzes the week's trades, identifies patterns, and suggests parameter adjustments. Requires analytics module (Phase 4) and sufficient trade history.

### 7.4 Config Audit Trail
- **Priority:** P4
- **Status:** [ ] Pending
- **Description:** Log config changes with old/new values, actor, timestamp, reason. Not needed until runtime config changes are possible (Phase 5 Telegram bot).

---

## Execution Order

```
PHASE 1 — Critical Safety (before market open Monday)
  1.1  Fix parameter values in settings.py
  1.2  Hard safety gate (core/safety.py)
  1.3  Trading loss circuit breakers
  1.4  Kill switch

PHASE 2 — Execution Safety
  2.1  Market open delay enforcement
  2.2  Market close buffer enforcement
  2.3  Max executions per day
  2.4  Earnings blackout enforcement
  2.5  Spread check enforcement
  2.6  Adaptive profit targets (DTE-based)
  2.7  Max hold DTE mandatory exit

PHASE 3 — Monitoring & Reconciliation
  3.1  Full position reconciliation
  3.2  Health check system
  3.3  Correlation IDs in logging

PHASE 4 — Analytics & Performance
  4.1  Performance module
  4.2  Enhanced daily summary
  4.3  Performance-aware agent prompts

PHASE 5 — Interactive Telegram Bot
  5.1  Telegram command handler

PHASE 6 — Reliability & Operations
  6.1  Retry/backoff for all services
  6.2  Structured JSON logging
  6.3  Database backup

PHASE 7 — Future (deferred)
  7.1  Dynamic equity-tier sizing
  7.2  DQN learning pipeline
  7.3  Weekly self-review agent
  7.4  Config audit trail
```

---

## Progress Tracker

| ID | Item | Phase | Priority | Status | Tests |
|----|------|-------|----------|--------|-------|
| 1.1 | Fix parameter values | 1 | P0 | [x] Done | [x] 79/79 |
| 1.2 | Hard safety gate | 1 | P0 | [x] Done | [x] 11 tests |
| 1.3 | Trading loss circuit breakers | 1 | P0 | [x] Done | [x] 5 tests |
| 1.4 | Kill switch | 1 | P0 | [x] Done | [x] 4 tests |
| 2.1 | Market open delay | 2 | P1 | [x] Done (in 1.2) | [x] via safety gate |
| 2.2 | Market close buffer | 2 | P1 | [x] Done (in 1.2) | [x] via safety gate |
| 2.3 | Max executions/day | 2 | P1 | [x] Done (in 1.2) | [x] via safety gate |
| 2.4 | Earnings blackout | 2 | P1 | [x] Done | [x] 3 tests |
| 2.5 | Spread check | 2 | P1 | [x] Done | [x] 3 tests |
| 2.6 | Adaptive profit targets | 2 | P1 | [x] Done | [x] 6 tests |
| 2.7 | Max hold DTE exit | 2 | P1 | [x] Done | [x] 3 tests |
| 3.1 | Position reconciliation | 3 | P1 | [x] Done | [x] 4 tests |
| 3.2 | Health check system | 3 | P2 | [x] Done | [x] 3 tests |
| 3.3 | Correlation IDs | 3 | P2 | [x] Done | [x] integrated |
| 4.1 | Performance module | 4 | P2 | [x] Done | [x] 14 tests |
| 4.2 | Enhanced daily summary | 4 | P2 | [x] Done | [x] integrated |
| 4.3 | Performance-aware prompts | 4 | P3 | [x] Done | [x] integrated |
| 5.1 | Telegram command handler | 5 | P3 | [x] Done | [x] 8 tests |
| 6.1 | Retry/backoff all services | 6 | P2 | [x] Done | [x] integrated |
| 6.2 | Structured JSON logging | 6 | P3 | [x] Done | [x] verified |
| 6.3 | Database backup | 6 | P3 | [x] Done | [x] cron active |
| 7.1 | Dynamic sizing | 7 | P4 | [ ] Deferred | [ ] |
| 7.2 | DQN learning | 7 | P4 | [ ] Deferred | [ ] |
| 7.3 | Weekly self-review | 7 | P4 | [ ] Deferred | [ ] |
| 7.4 | Config audit trail | 7 | P4 | [ ] Deferred | [ ] |

---

## Test Strategy

- Every phase gets its own test file: `tests/unit/test_safety.py`, `tests/unit/test_circuit_breaker.py`, etc.
- Run full suite after each phase: `pytest tests/ -v`
- E2E validation after Phase 1+2: `python -m scripts.run --once` with assertions
- Existing 57 tests must continue to pass (no regressions)

---

## Notes

- Phase 1 is the minimum viable safety layer. The system should NOT trade with real capital until Phase 1 is complete.
- Phase 2 items are enforced inside the SafetyGate from 1.2, so they build on that foundation.
- Phases 3-6 can be done in any order based on priority, but the sequence above is recommended.
- Phase 7 items require trade history and operational experience — defer until the system has been running for weeks.
