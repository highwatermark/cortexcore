# Critical + Medium Trading Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 6 bugs causing consistent paper trading losses — broken IV rank, dead spread gate, blind AI decisions, disabled sizing tool, premature signal dedup, and orphan conviction defaults.

**Architecture:** Each fix is isolated to 1-3 files. No architectural changes — just correcting existing logic. TDD: write failing test first, then fix.

**Tech Stack:** Python 3.11+, pytest, alpaca-py, pydantic, SQLAlchemy

**Test runner:** `.venv/bin/python -m pytest tests/ -v`

---

### Task 1: Fix `self._broker` AttributeError in Performance Context (Critical — simplest fix first)

**Files:**
- Modify: `agents/orchestrator.py:485`
- Test: `tests/unit/test_orchestrator_perf.py` (create)

**Step 1: Write the failing test**

Create `tests/unit/test_orchestrator_perf.py`:

```python
"""Tests for Orchestrator.get_performance_context()."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from agents.orchestrator import Orchestrator
from data.models import (
    IntentStatus,
    OrderIntent,
    OrderSide,
    SignalAction,
    TradeLog,
    init_db,
    get_session,
)


class TestPerformanceContext:
    def setup_method(self) -> None:
        init_db(":memory:")

    @patch("agents.orchestrator.get_broker")
    def test_returns_context_string(self, mock_get_broker) -> None:
        """get_performance_context should return a non-empty string with stats."""
        mock_get_broker.return_value.get_positions.return_value = []
        orch = Orchestrator()
        result = orch.get_performance_context()
        assert "PERFORMANCE CONTEXT" in result
        assert "Win rate" in result
        assert "Consecutive losses" in result

    @patch("agents.orchestrator.get_broker")
    def test_counts_consecutive_losses(self, mock_get_broker) -> None:
        """Consecutive losses should be reflected in context."""
        mock_get_broker.return_value.get_positions.return_value = []
        session = get_session()
        for i in range(3):
            session.add(TradeLog(
                position_id=f"pos-{i}", ticker="AAPL", action=SignalAction.CALL,
                entry_price=5.0, exit_price=4.0, quantity=1,
                pnl_dollars=-100, pnl_pct=-20, hold_duration_hours=8,
                opened_at=datetime.now(timezone.utc) - timedelta(minutes=30 * (3 - i)),
            ))
        session.commit()
        session.close()

        orch = Orchestrator()
        result = orch.get_performance_context()
        assert "Consecutive losses: 3" in result

    @patch("agents.orchestrator.get_broker")
    def test_open_positions_count(self, mock_get_broker) -> None:
        """Open positions count should match broker positions."""
        mock_positions = [MagicMock(), MagicMock()]
        mock_get_broker.return_value.get_positions.return_value = mock_positions
        orch = Orchestrator()
        result = orch.get_performance_context()
        assert "Open positions: 2" in result
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_perf.py -v`
Expected: FAIL — `self._broker` AttributeError causes empty string return, "PERFORMANCE CONTEXT" not found.

**Step 3: Fix the bug**

In `agents/orchestrator.py`, add import at top:

```python
from services.alpaca_broker import get_broker
```

Replace line 485:
```python
# OLD: open_positions = len(self._broker.get_positions())
# NEW:
open_positions = len(get_broker().get_positions())
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator_perf.py -v`
Expected: 3 PASS

**Step 5: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All existing tests still pass.

**Step 6: Commit**

```bash
git add tests/unit/test_orchestrator_perf.py agents/orchestrator.py
git commit -m "fix: replace self._broker with get_broker() in performance context

self._broker doesn't exist on Orchestrator, causing silent AttributeError.
Claude never received win rate, consecutive losses, or position count."
```

---

### Task 2: Add `bid`/`ask` Fields to FlowSignal and Extend Snapshots (Critical — prerequisite for spread gate fix)

**Files:**
- Modify: `data/models.py` (FlowSignal class)
- Modify: `services/alpaca_options_data.py` (get_snapshots return)
- Test: `tests/unit/test_market_context.py` (add snapshot test)

**Step 1: Write the failing test**

Add to `tests/unit/test_market_context.py`:

```python
class TestGetSnapshotsWithQuotes:
    def test_snapshots_include_bid_ask(self) -> None:
        """get_snapshots should return bid and ask prices from latest_quote."""
        from services.alpaca_options_data import AlpacaOptionsData
        from unittest.mock import patch, MagicMock

        mock_snap = MagicMock()
        mock_snap.latest_quote.bid_price = 3.50
        mock_snap.latest_quote.ask_price = 3.80
        mock_snap.latest_trade.price = 3.65
        mock_snap.greeks.delta = 0.5
        mock_snap.greeks.gamma = 0.02
        mock_snap.greeks.theta = -0.03
        mock_snap.greeks.vega = 0.1
        mock_snap.implied_volatility = 0.35

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._client = MagicMock()
            client._client.get_option_snapshot.return_value = {"AAPL260320C00200000": mock_snap}

            result = client.get_snapshots(["AAPL260320C00200000"])
            snap = result["AAPL260320C00200000"]
            assert snap["bid"] == 3.50
            assert snap["ask"] == 3.80
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_market_context.py::TestGetSnapshotsWithQuotes -v`
Expected: FAIL — KeyError on `snap["bid"]`.

**Step 3: Implement — add bid/ask to FlowSignal model**

In `data/models.py`, add to `FlowSignal` class after `next_earnings_date`:

```python
    # Bid/ask from Alpaca snapshot (for spread gate)
    bid: float = 0.0
    ask: float = 0.0
```

**Step 4: Implement — extend get_snapshots return**

In `services/alpaca_options_data.py`, inside `get_snapshots()`, update the results dict construction (around line 82-89) to include bid/ask:

```python
                bid_price = None
                ask_price = None
                if snap.latest_quote:
                    bid = snap.latest_quote.bid_price
                    ask = snap.latest_quote.ask_price
                    if bid and ask and bid > 0 and ask > 0:
                        current_price = round((bid + ask) / 2, 4)
                        bid_price = bid
                        ask_price = ask

                # ... existing latest_trade fallback ...

                greeks = snap.greeks
                results[symbol] = {
                    "current_price": current_price,
                    "bid": bid_price,
                    "ask": ask_price,
                    "delta": greeks.delta if greeks else None,
                    "gamma": greeks.gamma if greeks else None,
                    "theta": greeks.theta if greeks else None,
                    "vega": greeks.vega if greeks else None,
                    "iv": snap.implied_volatility,
                }
```

**Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_market_context.py::TestGetSnapshotsWithQuotes -v`
Expected: PASS

**Step 6: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All pass.

**Step 7: Commit**

```bash
git add data/models.py services/alpaca_options_data.py tests/unit/test_market_context.py
git commit -m "feat: add bid/ask to FlowSignal model and snapshot return

Prerequisite for activating the bid-ask spread safety gate.
Snapshots now include bid/ask from Alpaca latest_quote."
```

---

### Task 3: Activate Bid-Ask Spread Gate via IV Enrichment (Critical)

**Files:**
- Modify: `services/unusual_whales.py:200-208` (populate bid/ask during enrichment)
- Test: `tests/unit/test_safety.py` (add spread gate test with enriched signal)

**Step 1: Write the failing test**

Add to `tests/unit/test_safety.py` class `TestSafetyGate`:

```python
    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_spread_gate_blocks_wide_spread_from_enrichment(self, mock_timing, mock_broker) -> None:
        """Signals enriched with bid/ask from Alpaca snapshot should be blocked by spread gate."""
        gate = SafetyGate()
        # Simulate enriched signal: bid=2.00, ask=3.00 → spread = 33% > 15%
        allowed, reason = gate.check_entry(_base_signal(bid=2.00, ask=3.00))
        assert allowed is False
        assert "Spread" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_spread_gate_passes_tight_spread(self, mock_timing, mock_broker) -> None:
        """Signals with tight bid/ask spread should pass the spread gate."""
        gate = SafetyGate()
        # Simulate tight spread: bid=3.45, ask=3.55 → spread = 2.8% < 15%
        allowed, reason = gate.check_entry(_base_signal(bid=3.45, ask=3.55))
        assert allowed is True
```

**Step 2: Run test to verify they pass** (these should already pass — spread gate logic exists, it just never gets data)

Run: `.venv/bin/python -m pytest tests/unit/test_safety.py::TestSafetyGate::test_spread_gate_blocks_wide_spread_from_enrichment tests/unit/test_safety.py::TestSafetyGate::test_spread_gate_passes_tight_spread -v`
Expected: PASS (the gate logic already works, the bug is in the data path)

**Step 3: Populate bid/ask during IV enrichment**

In `services/unusual_whales.py`, inside the IV enrichment loop (around line 201-206), add bid/ask population:

```python
                    for sig in signals:
                        snap = snapshots.get(sig.option_symbol)
                        if snap and snap.get("iv") is not None:
                            sig.iv_rank = round(snap["iv"] * 100, 1)
                            enriched += 1
                        # Populate bid/ask for spread gate
                        if snap:
                            if snap.get("bid") is not None:
                                sig.bid = snap["bid"]
                            if snap.get("ask") is not None:
                                sig.ask = snap["ask"]
```

**Step 4: Write integration-style test for enrichment**

Add to `tests/unit/test_flow_tools.py`:

```python
class TestSignalEnrichment:
    def test_signal_with_bid_ask_flows_to_scoring(self) -> None:
        """Signals with bid/ask populated should carry those values through to dict."""
        from data.models import FlowSignal, SignalAction
        sig = FlowSignal(
            ticker="AAPL", action=SignalAction.CALL, strike=200.0,
            expiration="2026-04-17", premium=150000, volume=500,
            open_interest=300, vol_oi_ratio=1.67, option_type="CALL",
            bid=2.00, ask=3.00,
        )
        d = sig.model_dump()
        assert d["bid"] == 2.00
        assert d["ask"] == 3.00
```

**Step 5: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All pass.

**Step 6: Commit**

```bash
git add services/unusual_whales.py tests/unit/test_safety.py tests/unit/test_flow_tools.py
git commit -m "fix: populate bid/ask from Alpaca snapshots to activate spread gate

The spread safety gate existed but never fired because UW flow signals
don't include bid/ask. Now populated during IV enrichment from Alpaca
option snapshots. Wide-spread illiquid options will be blocked."
```

---

### Task 4: Compute True IV Rank from Historical Volatility (Critical)

**Files:**
- Modify: `services/alpaca_options_data.py` (new method + cache)
- Modify: `services/unusual_whales.py:204-205` (use new method)
- Test: `tests/unit/test_iv_rank.py` (create)

**Step 1: Write the failing test**

Create `tests/unit/test_iv_rank.py`:

```python
"""Tests for true IV rank computation from historical volatility."""
from __future__ import annotations

import math
from unittest.mock import patch, MagicMock

import pytest


class TestComputeIvRank:
    def test_iv_rank_at_50th_percentile(self) -> None:
        """Current IV at median of historical range should return ~50."""
        from services.alpaca_options_data import AlpacaOptionsData

        # Mock historical bars: 253 days of prices producing known vol distribution
        # With steady 1% daily returns, annualized vol ~ 15.87%
        # If current IV = 0.16 (close to annualized vol), rank should be ~50
        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._stock_client = None
            client._settings = MagicMock()
            client._settings.api.alpaca_api_key = "test"
            client._settings.api.alpaca_secret_key = "test"
            client._iv_rank_cache = {}

            # Build mock bars: linear price series $100...$125 over 253 days
            mock_bars = []
            for i in range(253):
                bar = MagicMock()
                bar.close = 100.0 * (1.001 ** i)  # ~0.1% daily return
                bar.timestamp = MagicMock()
                mock_bars.append(bar)

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars', return_value=mock_bars):
                rank = client.compute_iv_rank("AAPL", current_iv=0.02)
                # Very low IV relative to even a low-vol history
                assert rank < 30

                rank_high = client.compute_iv_rank("AAPL", current_iv=0.50)
                # Very high IV relative to history
                assert rank_high > 80

    def test_iv_rank_returns_zero_on_no_history(self) -> None:
        """If no historical data, return 0 (fail-open: low rank means trade is allowed)."""
        from services.alpaca_options_data import AlpacaOptionsData

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._iv_rank_cache = {}

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars', return_value=[]):
                rank = client.compute_iv_rank("AAPL", current_iv=0.35)
                assert rank == 0.0

    def test_iv_rank_cache_hit(self) -> None:
        """Second call for same ticker should use cache, not re-fetch."""
        from services.alpaca_options_data import AlpacaOptionsData
        from datetime import date

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            # Pre-populate cache with known vol distribution
            client._iv_rank_cache = {
                "AAPL": {
                    "date": date.today().isoformat(),
                    "vol_distribution": [0.15, 0.20, 0.25, 0.30, 0.35],
                }
            }

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars') as mock_fetch:
                rank = client.compute_iv_rank("AAPL", current_iv=0.22)
                mock_fetch.assert_not_called()
                # 0.22 is between 0.20 and 0.25 → around 40th percentile
                assert 20 < rank < 60
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_iv_rank.py -v`
Expected: FAIL — `compute_iv_rank` method doesn't exist.

**Step 3: Implement compute_iv_rank**

In `services/alpaca_options_data.py`, add these imports at the top:

```python
import math
from datetime import date, timedelta
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
```

Add instance variable in `__init__`:

```python
        self._iv_rank_cache: dict[str, dict] = {}
```

Add two methods to `AlpacaOptionsData`:

```python
    def _fetch_historical_bars(self, ticker: str, days: int = 365) -> list:
        """Fetch daily bars for a stock over the past N calendar days."""
        client = self._get_stock_client()
        end = date.today()
        start = end - timedelta(days=days)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Day,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            bars = client.get_stock_bars(request)
            return list(bars[ticker]) if ticker in bars else []
        except Exception as e:
            log.warning("historical_bars_failed", ticker=ticker, error=str(e))
            return []

    def compute_iv_rank(self, ticker: str, current_iv: float) -> float:
        """Compute IV rank as percentile of current IV vs 52-week realized vol.

        Returns 0-100 where 80 means current IV is higher than 80% of
        the historical realized volatility distribution.
        Returns 0.0 if insufficient data (fail-open: allows trade).
        """
        today_str = date.today().isoformat()

        # Check cache
        cached = self._iv_rank_cache.get(ticker)
        if cached and cached.get("date") == today_str and cached.get("vol_distribution"):
            vol_dist = cached["vol_distribution"]
        else:
            # Fetch and compute
            bars = self._fetch_historical_bars(ticker, days=365)
            if len(bars) < 60:  # Need at least 60 days for meaningful vol
                log.info("iv_rank_insufficient_data", ticker=ticker, bars=len(bars))
                return 0.0

            # Compute 30-day rolling realized vol (annualized)
            closes = [b.close for b in bars]
            log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            vol_dist = []
            window = 30
            for i in range(window, len(log_returns)):
                window_returns = log_returns[i - window:i]
                daily_vol = (sum(r ** 2 for r in window_returns) / window) ** 0.5
                annual_vol = daily_vol * math.sqrt(252)
                vol_dist.append(round(annual_vol, 4))

            if not vol_dist:
                return 0.0

            # Cache for today
            self._iv_rank_cache[ticker] = {
                "date": today_str,
                "vol_distribution": vol_dist,
            }

        # Percentile rank: what fraction of historical vol is below current IV
        below = sum(1 for v in vol_dist if v < current_iv)
        rank = (below / len(vol_dist)) * 100
        log.info("iv_rank_computed", ticker=ticker, current_iv=round(current_iv, 4),
                 rank=round(rank, 1), distribution_size=len(vol_dist))
        return round(rank, 1)
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_iv_rank.py -v`
Expected: 3 PASS

**Step 5: Wire it into IV enrichment**

In `services/unusual_whales.py`, replace the raw IV assignment (line 204-205):

```python
# OLD:
#   sig.iv_rank = round(snap["iv"] * 100, 1)
# NEW:
                            raw_iv = snap["iv"]
                            try:
                                iv_rank = get_options_data_client().compute_iv_rank(sig.ticker, raw_iv)
                                sig.iv_rank = iv_rank
                            except Exception as e_ivr:
                                log.warning("iv_rank_compute_failed", ticker=sig.ticker, error=str(e_ivr))
                                sig.iv_rank = 0.0  # fail-open
```

**Step 6: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All pass.

**Step 7: Commit**

```bash
git add services/alpaca_options_data.py services/unusual_whales.py tests/unit/test_iv_rank.py
git commit -m "fix: compute true IV rank from 52-week historical volatility

Previously iv_rank stored raw implied volatility (e.g. 35% IV as 35.0),
not IV percentile rank. A stock at 85th percentile IV historically
would pass the 70% gate if its absolute IV happened to be under 70%.

Now computes rolling 30-day realized vol over 252 trading days, ranks
current option IV against that distribution. Cached per ticker per day."
```

---

### Task 5: Re-enable `calculate_position_size` Tool for Claude (Medium)

**Files:**
- Modify: `agents/orchestrator.py` (uncomment tool, update prompt)

**Step 1: Uncomment the tool**

In `agents/orchestrator.py`, uncomment lines 37-54 (the `calculate_position_size` tool in `DECISION_TOOLS`):

```python
DECISION_TOOLS = [
    {
        "name": "calculate_position_size",
        "description": (
            "Calculate the maximum number of contracts for a new position based on "
            "current equity, existing exposure, and risk limits. Returns max_contracts "
            "and the limiting factor. Call this BEFORE execute_entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "option_price": {
                    "type": "number",
                    "description": "The option premium per contract (e.g., 2.50 for $250/contract)",
                },
            },
            "required": ["option_price"],
        },
    },
    # ... rest of tools ...
```

**Step 2: Update entry system prompt**

Replace the sizing instruction in `ENTRY_DECISION_SYSTEM`:

```python
# OLD:
# 1. Use the SIZING CONSTRAINTS provided to calculate quantity yourself:
#    quantity = floor(min(per_trade_cap, position_value_cap, remaining_capacity) / (option_price * 100))
#    If quantity is 0, SKIP (option too expensive for current limits).
# 2. Call execute_entry directly with your conviction (0-100), thesis, and quantity.

# NEW:
# 1. Call calculate_position_size(option_price) to get max_contracts.
#    If max_contracts is 0, SKIP (option too expensive for current limits).
# 2. Call execute_entry with quantity <= max_contracts, your conviction (0-100), and thesis.
```

**Step 3: Simplify `_get_sizing_context`**

Remove the manual formula from the context since Claude will use the tool:

```python
    def _get_sizing_context(self, positions: list[dict]) -> str:
        """Pre-compute position sizing constraints for Claude."""
        trading = self._settings.trading
        try:
            from services.alpaca_broker import get_broker
            account = get_broker().get_account()
            equity = account.get("equity", 0)
        except Exception:
            return "SIZING CONSTRAINTS:\n  (equity unavailable — skip all trades)\n"

        current_exposure = sum((p.get("entry_value", 0) or 0) for p in positions)
        max_total = equity * trading.max_total_exposure_pct
        remaining_capacity = max(0, max_total - current_exposure)

        return (
            f"SIZING CONSTRAINTS:\n"
            f"  Equity: ${equity:,.0f}\n"
            f"  Per-trade cap: ${equity * trading.max_per_trade_pct:,.0f}\n"
            f"  Position value cap: ${trading.max_position_value:,.0f}\n"
            f"  Remaining exposure capacity: ${remaining_capacity:,.0f}\n"
            f"  Use calculate_position_size(option_price) tool to get exact quantity.\n"
        )
```

Note: also fix `AlpacaBroker()` → `get_broker()` in `_get_sizing_context` (this is a secondary instance of the broker singleton bypass).

**Step 4: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All pass (no test changes needed — tool definitions aren't unit tested).

**Step 5: Commit**

```bash
git add agents/orchestrator.py
git commit -m "feat: re-enable calculate_position_size tool for Claude

Claude was computing position size via manual arithmetic from a formula
in the prompt. Now uses the battle-tested calculate_position_size tool
which handles all three sizing constraints correctly.

Also fixes AlpacaBroker() instantiation bypass in _get_sizing_context
to use get_broker() singleton."
```

---

### Task 6: Fix Signal Dedup — Mark at Acceptance, Not Scan Time (Medium)

**Files:**
- Modify: `tools/flow_tools.py` (dedup logic)
- Modify: `monitor/loop.py` (mark accepted signals)
- Test: `tests/unit/test_flow_tools.py` (add dedup tests)

**Step 1: Write the failing test**

Add to `tests/unit/test_flow_tools.py`:

```python
class TestSignalDedup:
    def setup_method(self) -> None:
        """Reset module-level dedup state between tests."""
        from tools import flow_tools
        flow_tools._seen_contracts.clear()
        flow_tools._seen_date = ""

    def test_rejected_signal_can_be_rescored(self) -> None:
        """A signal that was scanned but not accepted should be eligible for rescore."""
        from tools.flow_tools import _seen_contracts, _contract_key, mark_signal_accepted
        from data.models import FlowSignal, SignalAction

        sig = FlowSignal(
            ticker="AAPL", action=SignalAction.CALL, strike=200.0,
            expiration="2026-04-17", premium=150000, volume=500,
            open_interest=300, vol_oi_ratio=1.67, option_type="CALL",
        )
        key = _contract_key(sig)

        # After scan_flow processes it but it fails a gate, it should NOT
        # be marked as accepted
        _seen_contracts[key] = {"premium": sig.premium, "accepted": False}

        # On next scan, same signal should be allowed through
        entry = _seen_contracts.get(key)
        assert entry is not None
        assert entry["accepted"] is False
        # The scan_flow logic should allow rescore when accepted=False

    def test_accepted_signal_blocked_on_rescan(self) -> None:
        """A signal that was accepted should be blocked on rescan (same premium)."""
        from tools.flow_tools import _seen_contracts, _contract_key, mark_signal_accepted
        from data.models import FlowSignal, SignalAction

        sig = FlowSignal(
            ticker="AAPL", action=SignalAction.CALL, strike=200.0,
            expiration="2026-04-17", premium=150000, volume=500,
            open_interest=300, vol_oi_ratio=1.67, option_type="CALL",
        )
        key = _contract_key(sig)

        # Mark as accepted
        mark_signal_accepted(sig.model_dump())

        # Should be blocked
        entry = _seen_contracts.get(key)
        assert entry is not None
        assert entry["accepted"] is True

    def test_higher_premium_allows_rescore_even_if_accepted(self) -> None:
        """Higher premium should allow rescore even if previously accepted."""
        from tools.flow_tools import _seen_contracts, _contract_key, mark_signal_accepted
        from data.models import FlowSignal, SignalAction

        sig = FlowSignal(
            ticker="AAPL", action=SignalAction.CALL, strike=200.0,
            expiration="2026-04-17", premium=150000, volume=500,
            open_interest=300, vol_oi_ratio=1.67, option_type="CALL",
        )
        mark_signal_accepted(sig.model_dump())
        key = _contract_key(sig)
        entry = _seen_contracts[key]
        # Higher premium should pass the check
        assert 200000 > entry["premium"]
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_flow_tools.py::TestSignalDedup -v`
Expected: FAIL — `mark_signal_accepted` doesn't exist, `_seen_contracts` stores float not dict.

**Step 3: Implement dedup changes**

In `tools/flow_tools.py`:

1. Change `_seen_contracts` value type from `float` to `dict`:

```python
# OLD: _seen_contracts: dict[str, float] = {}
# NEW:
_seen_contracts: dict[str, dict] = {}
# Each value: {"premium": float, "accepted": bool}
```

2. Update `scan_flow()` dedup logic:

```python
    for sig in signals[: get_settings().flow.max_analyze]:
        key = _contract_key(sig)
        prev = _seen_contracts.get(key)
        if prev is not None:
            if prev["accepted"] and sig.premium <= prev["premium"]:
                # Already accepted with equal or higher premium — skip
                skipped += 1
                continue
            elif not prev["accepted"] and sig.premium <= prev["premium"]:
                # Previously rejected but not yet accepted — allow rescore
                pass
            elif sig.premium > prev["premium"]:
                # Higher premium — allow rescore regardless
                log.info(
                    "rescore_premium_increase",
                    contract=key,
                    old_premium=prev["premium"],
                    new_premium=sig.premium,
                )
        # Record as seen but not yet accepted
        _seen_contracts[key] = {"premium": sig.premium, "accepted": False}
        results.append(sig.model_dump())
```

3. Add `mark_signal_accepted()` function:

```python
def mark_signal_accepted(signal: dict) -> None:
    """Mark a signal's contract as accepted (passed all checks).

    Called from the monitor loop after a signal passes pre-trade checks.
    Accepted signals are blocked from rescore on subsequent cycles.
    """
    key = f"{signal.get('ticker', '')}:{signal.get('option_type', '')}:{signal.get('strike', 0)}:{signal.get('expiration', '')}"
    entry = _seen_contracts.get(key)
    if entry:
        entry["accepted"] = True
    else:
        _seen_contracts[key] = {"premium": signal.get("premium", 0), "accepted": True}
```

**Step 4: Update monitor loop to mark accepted signals**

In `monitor/loop.py`, after a signal passes pre-trade checks (around line 231):

```python
            for sig, score_result in scored:
                if not score_result.get("passed"):
                    continue
                ptc = pre_trade_check({**sig, "score": score_result.get("score", 0)}, risk_assessment)
                if ptc.get("approved"):
                    passing.append((sig, score_result, ptc))
                    # Mark signal as accepted so it won't be rescored
                    from tools.flow_tools import mark_signal_accepted
                    mark_signal_accepted(sig)
                else:
                    log.info("pre_trade_denied", ticker=sig.get("ticker"), reasons=ptc.get("reasons"))
```

**Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_flow_tools.py -v`
Expected: All pass including new dedup tests.

**Step 6: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All pass.

**Step 7: Commit**

```bash
git add tools/flow_tools.py monitor/loop.py tests/unit/test_flow_tools.py
git commit -m "fix: defer signal dedup marking to acceptance time, not scan time

Signals that fail temporary gates (market timing, cooldown) were
permanently lost because dedup marked them at scan time. Now signals
are marked 'seen but not accepted' at scan, and only marked 'accepted'
after passing pre-trade checks. Rejected signals are eligible for
rescore on the next cycle."
```

---

### Task 7: Fix Orphan Position Conviction Default (Medium)

**Files:**
- Modify: `tools/position_tools.py:104`
- Test: `tests/unit/test_position_tools.py` (add conviction default test)

**Step 1: Write the failing test**

Add to `tests/unit/test_position_tools.py`:

```python
class TestOrphanConviction:
    @patch("tools.position_tools.AlpacaBroker")
    def test_orphan_position_gets_default_conviction_75(self, mock_broker_cls) -> None:
        """Positions without DB records should get conviction=75, not 0."""
        init_db(":memory:")
        mock_pos = MagicMock()
        mock_pos.ticker = "AAPL"
        mock_pos.option_symbol = "AAPL260320C00200000"
        mock_pos.action = SignalAction.CALL
        mock_pos.strike = 200.0
        mock_pos.expiration = "2026-03-20"
        mock_pos.quantity = 2
        mock_pos.entry_price = 3.50
        mock_pos.current_price = 3.80
        mock_pos.pnl_pct = 8.57
        mock_pos.pnl_dollars = 60.0
        mock_pos.dte_remaining = 15
        mock_pos.position_id = "orphan-123"
        mock_broker_cls.return_value.get_positions.return_value = [mock_pos]

        positions = get_open_positions()
        assert len(positions) == 1
        assert positions[0]["conviction"] == 75
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_position_tools.py::TestOrphanConviction -v`
Expected: FAIL — `assertion 0 == 75` (current default is 0).

**Step 3: Fix the default**

In `tools/position_tools.py`, line 104:

```python
# OLD:
            "conviction": db_pos.conviction if db_pos else 0,
# NEW:
            "conviction": db_pos.conviction if db_pos else 75,
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_position_tools.py::TestOrphanConviction -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All pass.

**Step 6: Commit**

```bash
git add tools/position_tools.py tests/unit/test_position_tools.py
git commit -m "fix: default orphan position conviction to 75, not 0

Orphan positions (in broker but not DB) had conviction=0, which is
below the 50 exit threshold, immediately triggering CONVICTION_DROP
exit on adopted positions."
```

---

### Task 8: Update CLAUDE.md and Memory Files

**Files:**
- Modify: `CLAUDE.md`
- Modify: Memory file

**Step 1: Update CLAUDE.md**

Key changes:
- Update "13 deterministic safety gates" to reflect spread gate now active
- Update IV rank description: "IV rank computed as percentile of current IV vs 52-week historical realized volatility"
- Add lesson learned about raw IV vs IV rank
- Add lesson learned about dead spread gate
- Update "Key Settings" section: IV rank is now true percentile

**Step 2: Update memory file**

Add entries about:
- IV rank: computed from 52-week realized vol, not raw IV
- Spread gate: activated by populating bid/ask from Alpaca snapshots
- Performance context: uses `get_broker()` not `self._broker`
- Signal dedup: marks at acceptance, not scan time
- Orphan conviction: defaults to 75

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with fix documentation

- IV rank now computed as 52-week percentile
- Spread gate activated via Alpaca bid/ask enrichment
- Performance context bug documented in lessons learned
- Signal dedup behavior updated
- Orphan conviction default documented"
```

---

### Task 9: Final Verification

**Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass (existing + new).

**Step 2: Verify no regressions**

Run: `.venv/bin/python -m pytest tests/ -v --tb=short`
Expected: 0 failures.
