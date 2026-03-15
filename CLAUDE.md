# Momentum Agent v2 — Project Instructions

## Core Principles (Non-Negotiable)

1. **Alpaca broker is the ONLY source of truth for positions.** The database is a journal for logging, trade history, and metadata enrichment. Every decision path — safety gates, risk scoring, position sizing, exit triggers, Claude context — MUST query the broker first. If broker says 0 positions, there are 0 positions, regardless of what the DB says.

2. **Safety gates are deterministic and non-overridable.** The 14 checks in `core/safety.py` cannot be bypassed by AI conviction, special signals, or any prompt. If the gate says no, the trade does not happen.

3. **Fail-closed on safety, fail-open on data.** Missing earnings data? Allow the trade (fail-open). Safety check can't verify equity? Block the trade (fail-closed). When in doubt, block.

4. **Signal dedup marks signals as "seen" at scan time, "accepted" at pre-trade check time.** Signals that fail temporary gates (market timing, cooldown) are eligible for rescore on the next cycle. Only signals that pass all pre-trade checks are permanently blocked from rescore. Higher premium always allows rescore.

5. **Do not add complexity without evidence.** No speculative abstractions. No "future-proofing." If a three-line solution works, use it. The system trades real money — simplicity is safety.

## Architecture Rules

### Broker-First Position Queries
Every function that makes DECISIONS based on positions MUST call `broker.get_positions()`:
- `core/safety.py` — _check_max_positions, _check_max_exposure
- `tools/risk_tools.py` — calculate_portfolio_risk
- `tools/execution_tools.py` — calculate_position_size
- `tools/position_tools.py` — get_open_positions
- `monitor/loop.py` — _get_scan_interval, _send_daily_summary
- `agents/orchestrator.py` — get_performance_context

DB is ONLY used to enrich broker data with: Greeks, conviction, entry thesis, signal_id.

### What the DB IS For
- `trade_log` — P&L history for circuit breakers and analytics
- `order_intents` — idempotency (prevent duplicate orders)
- `signals` — flow scanner history
- `broker_orders` — order tracking for reconciliation
- `positions` — journal of entries/exits, Greeks storage, thesis metadata

### Reconciler Role
The reconciler (`core/reconciler.py`) syncs DB with broker every 5 cycles. It has 5 safety guards:
1. Check for existing TradeLog before phantom closure
2. Check for pending exit intent before phantom closure
3. Check for working sell order before phantom closure
4. Block same-cycle orphan re-adoption
5. Block recently-closed (30 min) orphan re-adoption

## Anti-Patterns (DO NOT)

- **DO NOT** query `PositionRecord.status == OPEN` for decisions — query broker
- **DO NOT** add AI-overridable safety checks — all safety is deterministic in `core/safety.py`
- **DO NOT** retry order submissions — idempotency risk; only retry data reads
- **DO NOT** add temporary blocking gates without considering signal dedup permanence
- **DO NOT** use `market_open_delay` > 0 — it blocks first-scan signals permanently (dedup)
- **DO NOT** assume DB is in sync with broker — they can diverge for up to 60 seconds
- **DO NOT** create phantom closures without checking for pending exits first
- **DO NOT** adopt orphan positions that were recently closed (within 30 minutes)
- **DO NOT** duplicate circuit breaker checks in the safety gate — circuit breakers have cooldowns that auto-clear; safety gate checks block permanently and can deadlock the system
- **DO NOT** add any safety gate check that requires a trade to happen before it can clear — if the check blocks entries, and only a new trade can clear it, the system deadlocks forever

## Key Settings (Authoritative)

- Max 3 concurrent positions, $1K max per position, 25% total exposure
- Max 2 executions per day, 20% max per trade as % of equity
- Profit targets: 40%/35%/25%/15% (adaptive by DTE >14/7-14/3-7/<3)
- Stop loss: -35%, mandatory exit at DTE <= 5
- Circuit breakers: 5% daily loss, 10% weekly loss, 2 consecutive losses (120 min cooldown)
- **Calls only, ASK-side only** — no puts, no BID-side
- DTE 6+, no upper limit (LEAPs allowed), premium $50-$500/contract, IV rank < 70% (true percentile vs 52-week realized vol)
- Spread gate: blocks entries with bid-ask spread > 15% (bid/ask populated from Alpaca snapshots during IV enrichment)

## Common Tasks

```bash
# Service management
sudo systemctl restart momentum-agent
sudo systemctl status momentum-agent
journalctl -u momentum-agent -f

# Single scan cycle (testing)
.venv/bin/python -m scripts.run --once

# Risk assessment only
.venv/bin/python -m scripts.run --risk

# Kill switch
.venv/bin/python -m scripts.run --kill      # halt all trading
.venv/bin/python -m scripts.run --unkill    # resume

# Run tests
.venv/bin/python -m pytest tests/ -v
```

## Before Making Changes

1. Run full test suite: `.venv/bin/python -m pytest tests/ -v` (195 passing, 2 pre-existing flaky DTE tests)
2. Check `docs/IMPLEMENTATION_PLAN.md` for related phase
3. If modifying position logic: verify it queries broker first, then DB for enrichment
4. If adding new gate: add to `core/safety.py`, test both allow and block paths
5. If touching circuit breaker: test loss sequences
6. Never add features/refactors beyond what was asked — keep changes minimal and focused

## Project Layout

```
config/settings.py          # Single source of truth for all parameters
core/safety.py              # 13 deterministic safety gates (non-overridable, spread gate now active)
core/circuit_breaker.py     # Loss-based trading halts
core/reconciler.py          # Broker <-> DB sync with 5 safety guards
core/killswitch.py          # Emergency halt
tools/position_tools.py     # Broker-first position queries
tools/risk_tools.py         # Broker-first risk scoring
tools/execution_tools.py    # Entry/exit with idempotency + sizing
monitor/loop.py             # Main event loop
agents/orchestrator.py      # Claude multi-agent coordinator
bot/commands.py             # Telegram bot (15 commands)
analytics/performance.py    # Win rate, Sharpe, drawdown
```

## Lessons Learned (Hard-Won)

- Broker-first architecture: DB was originally source of truth — caused phantom closures, stale position counts, blocked valid entries. Fixed 2026-03-02.
- Reconciler death spiral: phantom→orphan→phantom loop caused by no guards. Fixed with 5 checks.
- market_open_delay at 15 min blocked ALL first-scan signals permanently (dedup marks at scan time). Set to 0.
- Venv deps not installed after server restart: always `pip install -e ".[dev]"` after reboot.
- Mock paths: use `services.alpaca_broker.get_broker` not `core.safety.get_broker` for patching.
- **Safety gate consecutive-loss deadlock (fixed 2026-03-14):** `_check_consecutive_losses` in `safety.py` had NO cooldown — it permanently blocked all entries when last 2 trades were losses. Since no new trades could happen, the counter never cleared, creating a permanent deadlock. The circuit breaker in `circuit_breaker.py` already handles this with a 120-min cooldown. Removed the duplicate check from the safety gate. Rule: never put a check in the safety gate that requires a trade to clear it.
- systemd `StartLimitIntervalSec` goes in [Unit] not [Service].
- **IV rank was raw IV, not percentile (fixed 2026-03-14):** `sig.iv_rank = round(snap["iv"] * 100, 1)` stored raw implied volatility as "IV rank". A stock with 35% IV got iv_rank=35.0 regardless of whether that was historically cheap or expensive. Now computes true IV rank as percentile of current IV vs 52-week rolling realized vol. Cached per ticker per day.
- **Spread gate was dead (fixed 2026-03-14):** The `_check_spread` gate in safety.py checked `signal.get("bid", 0)` but UW flow signals never include bid/ask. Gate always returned `(True, "")`. Now bid/ask are populated from Alpaca option snapshots during IV enrichment. Illiquid options with wide spreads are now blocked.
- **Performance context silently crashed (fixed 2026-03-14):** `self._broker.get_positions()` in `orchestrator.py` referenced a non-existent attribute. Exception caught silently, Claude never saw win rate, consecutive losses, or drawdown. Fixed to use `get_broker()`.
- **Signal dedup at scan time lost good signals (fixed 2026-03-14):** Signals failing temporary gates were permanently lost. Changed to two-phase: "seen" at scan, "accepted" at pre-trade check. Rejected signals eligible for rescore.
- **Orphan conviction default was 0 (fixed 2026-03-14):** Orphan positions (in broker but not DB) got conviction=0, immediately triggering CONVICTION_DROP exit (threshold 50). Changed default to 75.
