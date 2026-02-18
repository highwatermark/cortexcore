"""Tests for the hard safety gate and circuit breakers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from core.safety import SafetyGate
from core.circuit_breaker import TradingCircuitBreaker, BreakerState
from core.killswitch import is_killed, engage, disengage, KILLSWITCH_PATH
from data.models import (
    IntentStatus,
    OrderIntent,
    OrderSide,
    PositionRecord,
    PositionStatus,
    SignalAction,
    TradeLog,
    init_db,
    get_session,
)


def _base_signal(**overrides) -> dict:
    sig = {
        "signal_id": "test-sig-1",
        "ticker": "AAPL",
        "option_symbol": "AAPL260320C00200000",
        "quantity": 1,
        "limit_price": 3.50,
        "iv_rank": 30,
        "dte": 30,
    }
    sig.update(overrides)
    return sig


def _mock_broker_account(equity=100_000):
    mock = MagicMock()
    mock.get_account.return_value = {"equity": equity}
    return mock


class TestSafetyGate:
    def setup_method(self) -> None:
        init_db(":memory:")

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_clean_signal_passes(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal())
        assert allowed is True

    def test_excluded_ticker_blocked(self) -> None:
        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal(ticker="SPY"))
        assert allowed is False
        assert "excluded" in reason.lower()

    def test_excluded_ticker_gme(self) -> None:
        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal(ticker="GME"))
        assert allowed is False

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    def test_max_positions_blocked(self, mock_broker) -> None:
        session = get_session()
        for i in range(3):
            session.add(PositionRecord(
                position_id=f"pos-{i}", signal_id=f"sig-{i}", ticker="AAPL",
                option_symbol=f"AAPL{i}", action=SignalAction.CALL,
                strike=200, expiration="2026-03-20", quantity=1,
                entry_price=3.0, entry_value=300, status=PositionStatus.OPEN,
            ))
        session.commit()
        session.close()

        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal())
        assert allowed is False
        assert "Max positions" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account(equity=5000))
    def test_max_exposure_blocked(self, mock_broker) -> None:
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-1", signal_id="sig-1", ticker="NVDA",
            option_symbol="NVDA1", action=SignalAction.CALL,
            strike=200, expiration="2026-03-20", quantity=1,
            entry_price=10.0, entry_value=1000, status=PositionStatus.OPEN,
        ))
        session.commit()
        session.close()

        gate = SafetyGate()
        # Trying to add $350 (1 contract * $3.50 * 100) to $1000 existing = $1350
        # On $5K equity that's 27% > 25% limit
        allowed, reason = gate.check_entry(_base_signal())
        assert allowed is False
        assert "exposure" in reason.lower()

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_max_position_value_blocked(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        # 2 contracts * $6.00 * 100 = $1200 > $1000 limit
        allowed, reason = gate.check_entry(_base_signal(quantity=2, limit_price=6.00))
        assert allowed is False
        assert "Trade value" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    def test_max_executions_today_blocked(self, mock_broker) -> None:
        session = get_session()
        for i in range(2):
            session.add(OrderIntent(
                idempotency_key=f"entry-sig-{i}", signal_id=f"sig-{i}",
                ticker="AAPL", option_symbol="AAPL1", side=OrderSide.BUY,
                quantity=1, status=IntentStatus.EXECUTED,
                executed_at=datetime.now(timezone.utc),
            ))
        session.commit()
        session.close()

        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal())
        assert allowed is False
        assert "Max entries today" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    def test_daily_loss_blocked(self, mock_broker) -> None:
        session = get_session()
        # $6000 loss on $100K equity = 6% > 5%
        session.add(TradeLog(
            position_id="pos-1", ticker="TSLA", action=SignalAction.CALL,
            entry_price=5.0, exit_price=2.0, quantity=20,
            pnl_dollars=-6000, pnl_pct=-60, hold_duration_hours=4,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=5),
        ))
        session.commit()
        session.close()

        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal())
        assert allowed is False
        assert "Daily loss" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    def test_consecutive_losses_blocked(self, mock_broker) -> None:
        session = get_session()
        for i in range(2):
            session.add(TradeLog(
                position_id=f"pos-{i}", ticker="AAPL", action=SignalAction.CALL,
                entry_price=5.0, exit_price=4.0, quantity=1,
                pnl_dollars=-100, pnl_pct=-20, hold_duration_hours=8,
                opened_at=datetime.now(timezone.utc) - timedelta(hours=24),
            ))
        session.commit()
        session.close()

        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal())
        assert allowed is False
        assert "losses" in reason.lower()

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_iv_rank_blocked(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal(iv_rank=80))
        assert allowed is False
        assert "IV rank" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_dte_blocked(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        allowed, reason = gate.check_entry(_base_signal(dte=3))
        assert allowed is False
        assert "DTE" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_spread_blocked(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        # Spread = (5.00 - 4.00) / 5.00 = 20% > 15% limit
        allowed, reason = gate.check_entry(_base_signal(bid=4.00, ask=5.00))
        assert allowed is False
        assert "Spread" in reason

    def test_spread_allowed(self) -> None:
        gate = SafetyGate()
        # Spread = (3.60 - 3.50) / 3.60 = 2.8% < 15% limit
        allowed, reason = gate.check_entry(_base_signal(bid=3.50, ask=3.60))
        # May still be blocked by market timing, so just check spread doesn't block
        if not allowed:
            assert "Spread" not in reason

    def test_spread_skipped_when_no_data(self) -> None:
        gate = SafetyGate()
        # No bid/ask in signal — spread check should be skipped (fail-open)
        allowed, reason = gate.check_entry(_base_signal())
        # Should not be blocked by spread (may be blocked by other checks)
        if not allowed:
            assert "Spread" not in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_earnings_blackout_blocked(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        # Earnings tomorrow — within 2-day blackout
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        allowed, reason = gate.check_entry(_base_signal(ticker="AAPL", next_earnings_date=tomorrow))
        assert allowed is False
        assert "Earnings blackout" in reason

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_earnings_far_away_allowed(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        # Earnings 30 days away, well outside 2-day blackout
        far_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        allowed, reason = gate.check_entry(_base_signal(ticker="AAPL", next_earnings_date=far_date))
        assert allowed is True

    @patch("services.alpaca_broker.get_broker", return_value=_mock_broker_account())
    @patch.object(SafetyGate, "_check_market_timing", return_value=(True, ""))
    def test_earnings_no_data_allowed(self, mock_timing, mock_broker) -> None:
        gate = SafetyGate()
        # No earnings data — fail-open
        allowed, reason = gate.check_entry(_base_signal(ticker="AAPL"))
        assert allowed is True


class TestTradingCircuitBreaker:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_no_trades_clear(self) -> None:
        breaker = TradingCircuitBreaker()
        state = breaker.check(100_000)
        assert state.is_tripped is False

    def test_daily_loss_trips(self) -> None:
        session = get_session()
        session.add(TradeLog(
            position_id="pos-1", ticker="TSLA", action=SignalAction.CALL,
            entry_price=5.0, exit_price=2.0, quantity=20,
            pnl_dollars=-6000, pnl_pct=-60, hold_duration_hours=4,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=5),
        ))
        session.commit()
        session.close()

        breaker = TradingCircuitBreaker()
        state = breaker.check(100_000)
        assert state.is_tripped is True
        assert "Daily loss" in state.reason

    def test_weekly_loss_trips(self) -> None:
        session = get_session()
        for i in range(3):
            session.add(TradeLog(
                position_id=f"pos-{i}", ticker="AAPL", action=SignalAction.CALL,
                entry_price=5.0, exit_price=1.0, quantity=10,
                pnl_dollars=-4000, pnl_pct=-80, hold_duration_hours=24,
                opened_at=datetime.now(timezone.utc) - timedelta(days=i),
            ))
        session.commit()
        session.close()

        breaker = TradingCircuitBreaker()
        state = breaker.check(100_000)
        assert state.is_tripped is True

    def test_consecutive_losses_trips(self) -> None:
        session = get_session()
        for i in range(2):
            session.add(TradeLog(
                position_id=f"pos-{i}", ticker="AAPL", action=SignalAction.CALL,
                entry_price=5.0, exit_price=4.0, quantity=1,
                pnl_dollars=-100, pnl_pct=-20, hold_duration_hours=8,
                opened_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            ))
        session.commit()
        session.close()

        breaker = TradingCircuitBreaker()
        state = breaker.check(100_000)
        assert state.is_tripped is True
        assert "losses" in state.reason.lower()

    def test_wins_clear_consecutive(self) -> None:
        session = get_session()
        # One loss then one win
        session.add(TradeLog(
            position_id="pos-0", ticker="AAPL", action=SignalAction.CALL,
            entry_price=5.0, exit_price=4.0, quantity=1,
            pnl_dollars=-100, pnl_pct=-20, hold_duration_hours=8,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
            closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        session.add(TradeLog(
            position_id="pos-1", ticker="NVDA", action=SignalAction.CALL,
            entry_price=5.0, exit_price=7.0, quantity=1,
            pnl_dollars=200, pnl_pct=40, hold_duration_hours=8,
            opened_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        ))
        session.commit()
        session.close()

        breaker = TradingCircuitBreaker()
        state = breaker.check(100_000)
        assert state.is_tripped is False


class TestKillSwitch:
    def setup_method(self) -> None:
        if KILLSWITCH_PATH.exists():
            KILLSWITCH_PATH.unlink()

    def teardown_method(self) -> None:
        if KILLSWITCH_PATH.exists():
            KILLSWITCH_PATH.unlink()

    def test_not_killed_by_default(self) -> None:
        assert is_killed() is False

    def test_engage_creates_file(self) -> None:
        engage("test reason")
        assert KILLSWITCH_PATH.exists()
        assert is_killed() is True

    def test_disengage_removes_file(self) -> None:
        engage("test")
        disengage()
        assert is_killed() is False

    def test_disengage_when_not_engaged(self) -> None:
        disengage()  # Should not error
        assert is_killed() is False
