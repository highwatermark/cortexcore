# Critical + Medium Fixes — Design Document

Date: 2026-03-14
Status: Approved

## Problem Statement

The momentum-agent-v2 options trading system is consistently losing money in paper trading due to three critical bugs and several medium-severity issues that compound to create a structural disadvantage on every trade.

## Root Cause Summary

1. **IV rank measures raw IV, not percentile rank** — buying expensive premium
2. **Bid-ask spread gate is dead** — entering illiquid options with 20-40% spreads
3. **Performance context silently crashes** — Claude decides blind to losing streaks
4. **Position sizing tool disabled** — Claude does manual arithmetic
5. **Signal dedup at scan time** — good signals permanently lost on temporary gates
6. **Orphan conviction defaults to 0** — triggers immediate exit

---

## Fix 1: True IV Rank from Historical Volatility (Critical)

**File:** `services/alpaca_options_data.py`, `services/unusual_whales.py`

**Problem:** `sig.iv_rank = round(snap["iv"] * 100, 1)` stores raw IV as "IV rank". A stock with 35% IV gets iv_rank=35.0 regardless of whether that's historically cheap or expensive.

**Solution:** Compute true IV rank as a percentile of current IV vs 52-week historical realized volatility distribution using Alpaca's stock historical data.

- New method `AlpacaOptionsData.compute_iv_rank(ticker, current_iv)` that:
  1. Fetches 252 trading days of daily closes for the underlying stock
  2. Computes rolling 30-day annualized realized volatility for each window
  3. Ranks current option IV against this distribution
  4. Returns percentile 0-100
- Cache the historical vol distribution per ticker (dict, refreshed daily)
- Replace the raw IV assignment in `unusual_whales.py` with `compute_iv_rank()` call
- Keep threshold at 70 in settings (now means "IV is in the 70th percentile or higher historically")

**Trade-offs:** Adds one Alpaca stock bars API call per unique ticker per day (cached). More accurate than raw IV.

---

## Fix 2: Activate Bid-Ask Spread Gate (Critical)

**Files:** `services/alpaca_options_data.py`, `services/unusual_whales.py`, `data/models.py`

**Problem:** Spread gate checks `signal.get("bid", 0)` but UW signals never include bid/ask. Gate always returns `(True, "")`.

**Solution:**
- Extend `get_snapshots()` return dict to include `bid` and `ask` from `snap.latest_quote`
- Add `bid` and `ask` optional fields to `FlowSignal` model in `data/models.py`
- During IV enrichment in `unusual_whales.py`, populate `sig.bid` and `sig.ask` from snapshot data
- The existing spread gate in `safety.py` requires zero changes — it already checks bid/ask correctly

**Trade-offs:** No additional API calls (data already fetched in existing snapshot call).

---

## Fix 3: Fix `self._broker` AttributeError (Critical)

**File:** `agents/orchestrator.py`

**Problem:** `self._broker.get_positions()` on line 485 references a non-existent attribute. The exception is silently caught, returning empty string for performance context. Claude never sees consecutive losses, win rate, or drawdown.

**Solution:** Replace `self._broker.get_positions()` with `get_broker().get_positions()` (import `get_broker` from `services.alpaca_broker`).

---

## Fix 4: Re-enable `calculate_position_size` Tool (Medium)

**File:** `agents/orchestrator.py`

**Problem:** The `calculate_position_size` tool is commented out from `DECISION_TOOLS`. Claude computes quantity from a formula in the prompt, which may have arithmetic errors.

**Solution:**
- Uncomment the tool definition in `DECISION_TOOLS`
- Update `ENTRY_DECISION_SYSTEM` prompt to instruct Claude to call the tool
- Simplify `_get_sizing_context()` to show constraints without the manual formula

---

## Fix 5: Dedup Signals at Acceptance, Not Scan Time (Medium)

**Files:** `tools/flow_tools.py`, `monitor/loop.py`

**Problem:** `_seen_contracts[key] = sig.premium` is set during `scan_flow()` before scoring or gate checks. If a signal fails a temporary gate (market timing), it's permanently lost.

**Solution:**
- In `scan_flow()`: still check `_seen_contracts` but only skip if value is `True` (accepted)
- Store premium as before for high-watermark comparison, but mark acceptance separately
- New function `mark_signal_accepted(signal)` called from `monitor/loop.py` after a signal passes pre-trade checks
- Signals that fail temporary gates remain eligible for rescore on next cycle

---

## Fix 6: Fix Orphan Conviction Default (Medium)

**File:** `tools/position_tools.py`

**Problem:** `"conviction": db_pos.conviction if db_pos else 0` gives orphan positions conviction=0, which is below the 50 exit threshold, triggering immediate CONVICTION_DROP exit.

**Solution:** Change default from `0` to `75` (safely above the 50 threshold, won't trigger premature exit). Log when using default.

---

## Documentation Updates

- `CLAUDE.md`: Update safety gate documentation, IV rank description, spread gate status
- Memory file: Record fixes and patterns learned
