"""Tests for execution tools — position sizing."""
from unittest.mock import MagicMock, patch

import pytest

from tools.execution_tools import calculate_position_size


def _mock_session_no_positions():
    """Mock get_session that returns no open positions."""
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []
    return session


def _mock_session_with_exposure(exposure_value: float):
    """Mock get_session that returns positions with given total exposure."""
    session = MagicMock()
    pos = MagicMock()
    pos.entry_value = exposure_value
    session.query.return_value.filter.return_value.all.return_value = [pos]
    return session


class TestCalculatePositionSize:
    """Test deterministic position sizing logic."""

    @patch("tools.execution_tools.get_session")
    def test_basic_sizing_with_equity(self, mock_get_session):
        """With $10K equity, $2.50 option → cost $250/contract."""
        mock_get_session.return_value = _mock_session_no_positions()

        result = calculate_position_size(option_price=2.50, equity=10_000)

        assert result["max_contracts"] > 0
        assert result["cost_per_contract"] == 250.0
        assert result["equity"] == 10_000
        assert result["current_exposure"] == 0
        # per_trade_pct: 10000 * 0.20 / 250 = 8
        # position_value_cap: 1000 / 250 = 4
        # total_exposure: 10000 * 0.25 / 250 = 10
        # min(8, 4, 10) = 4
        assert result["max_contracts"] == 4
        assert result["limiting_factor"] == "position_value_cap"

    @patch("tools.execution_tools.get_session")
    def test_cheap_option_position_value_caps(self, mock_get_session):
        """Cheap option ($0.50 = $50/contract), position value $1K → 20 contracts max,
        but per_trade_pct or position_value_cap will limit."""
        mock_get_session.return_value = _mock_session_no_positions()

        result = calculate_position_size(option_price=0.50, equity=10_000)

        # per_trade_pct: 10000 * 0.20 / 50 = 40
        # position_value_cap: 1000 / 50 = 20
        # total_exposure: 10000 * 0.25 / 50 = 50
        # min(40, 20, 50) = 20
        assert result["max_contracts"] == 20
        assert result["limiting_factor"] == "position_value_cap"

    @patch("tools.execution_tools.get_session")
    def test_expensive_option_per_trade_limits(self, mock_get_session):
        """Expensive option ($5.00 = $500/contract)."""
        mock_get_session.return_value = _mock_session_no_positions()

        result = calculate_position_size(option_price=5.00, equity=10_000)

        # per_trade_pct: 10000 * 0.20 / 500 = 4
        # position_value_cap: 1000 / 500 = 2
        # total_exposure: 10000 * 0.25 / 500 = 5
        # min(4, 2, 5) = 2
        assert result["max_contracts"] == 2
        assert result["limiting_factor"] == "position_value_cap"

    @patch("tools.execution_tools.get_session")
    def test_existing_exposure_reduces_capacity(self, mock_get_session):
        """With existing $2K exposure out of $2.5K max (25% of $10K), only $500 left."""
        mock_get_session.return_value = _mock_session_with_exposure(2000)

        result = calculate_position_size(option_price=2.50, equity=10_000)

        # per_trade_pct: 10000 * 0.20 / 250 = 8
        # position_value_cap: 1000 / 250 = 4
        # total_exposure: (10000 * 0.25 - 2000) / 250 = 500 / 250 = 2
        # min(8, 4, 2) = 2
        assert result["max_contracts"] == 2
        assert result["limiting_factor"] == "total_exposure"
        assert result["remaining_capacity"] == 500.0

    @patch("tools.execution_tools.get_session")
    def test_no_capacity_remaining(self, mock_get_session):
        """Fully exposed — no room for new positions."""
        mock_get_session.return_value = _mock_session_with_exposure(2500)

        result = calculate_position_size(option_price=2.50, equity=10_000)

        assert result["max_contracts"] == 0
        assert result["limiting_factor"] == "total_exposure"

    @patch("tools.execution_tools.get_session")
    def test_over_exposed_returns_zero(self, mock_get_session):
        """If current exposure exceeds limit, returns 0."""
        mock_get_session.return_value = _mock_session_with_exposure(5000)

        result = calculate_position_size(option_price=2.50, equity=10_000)

        assert result["max_contracts"] == 0

    def test_zero_equity(self):
        """Zero equity returns error."""
        result = calculate_position_size(option_price=2.50, equity=0)
        assert result["max_contracts"] == 0
        assert "error" in result

    def test_zero_option_price(self):
        """Zero option price returns error."""
        result = calculate_position_size(option_price=0, equity=10_000)
        assert result["max_contracts"] == 0
        assert "error" in result

    def test_negative_price(self):
        """Negative option price returns error."""
        result = calculate_position_size(option_price=-1.0, equity=10_000)
        assert result["max_contracts"] == 0
        assert "error" in result

    @patch("tools.execution_tools.AlpacaBroker")
    @patch("tools.execution_tools.get_session")
    def test_fetches_equity_from_broker(self, mock_get_session, mock_broker_cls):
        """When equity not provided, fetches from broker."""
        mock_get_session.return_value = _mock_session_no_positions()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = {"equity": 10_000}
        mock_broker_cls.return_value = mock_broker

        result = calculate_position_size(option_price=2.50)

        assert result["max_contracts"] == 4
        assert result["equity"] == 10_000
        mock_broker.get_account.assert_called_once()

    @patch("tools.execution_tools.AlpacaBroker")
    def test_broker_error_returns_zero(self, mock_broker_cls):
        """If broker call fails, returns 0 with error."""
        mock_broker_cls.return_value.get_account.side_effect = Exception("API down")

        result = calculate_position_size(option_price=2.50)

        assert result["max_contracts"] == 0
        assert "error" in result


class TestExecuteEntrySizingIntegration:
    """Test that execute_entry respects position sizing."""

    @patch("tools.execution_tools.get_session")
    @patch("tools.execution_tools.calculate_position_size")
    @patch("tools.execution_tools.get_safety_gate")
    def test_entry_blocked_when_no_capacity(self, mock_gate, mock_sizing, mock_session):
        """execute_entry returns error when position sizing says 0."""
        import asyncio
        from tools.execution_tools import execute_entry

        mock_sizing.return_value = {"max_contracts": 0, "limiting_factor": "total_exposure"}

        result = asyncio.run(execute_entry(
            signal_id="sig-001",
            ticker="AAPL",
            option_symbol="AAPL250321C00200000",
            side="BUY",
            quantity=5,
            limit_price=2.50,
        ))

        assert result["success"] is False
        assert "Position sizing" in result["error"]
        mock_gate.assert_not_called()  # Should not even reach safety gate

    @patch("tools.execution_tools.TelegramNotifier")
    @patch("tools.execution_tools.AlpacaBroker")
    @patch("tools.execution_tools.get_session")
    @patch("tools.execution_tools.calculate_position_size")
    @patch("tools.execution_tools.get_safety_gate")
    def test_entry_quantity_capped(self, mock_gate, mock_sizing, mock_get_session, mock_broker_cls, mock_notifier):
        """execute_entry caps quantity when request exceeds max_contracts."""
        import asyncio
        from tools.execution_tools import execute_entry

        mock_sizing.return_value = {"max_contracts": 2, "limiting_factor": "position_value_cap"}

        # Safety gate passes
        gate = MagicMock()
        gate.check_entry.return_value = (True, "")
        mock_gate.return_value = gate

        # DB session
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None  # No duplicate
        mock_get_session.return_value = session

        # Broker
        broker = MagicMock()
        order_result = MagicMock()
        order_result.success = True
        order_result.broker_order_id = "order-123"
        broker.submit_limit_order.return_value = order_result
        broker.get_order_status.return_value = {"status": "filled", "filled_qty": 2, "filled_avg_price": 2.50}
        mock_broker_cls.return_value = broker

        # Notifier
        notifier = MagicMock()

        async def _noop(**kw):
            pass

        notifier.notify_entry = MagicMock(side_effect=_noop)
        mock_notifier.return_value = notifier

        result = asyncio.run(execute_entry(
            signal_id="sig-002",
            ticker="AAPL",
            option_symbol="AAPL250321C00200000",
            side="BUY",
            quantity=10,  # Requesting 10, but max is 2
            limit_price=2.50,
        ))

        # Safety gate should have been called with capped quantity
        call_args = gate.check_entry.call_args[0][0]
        assert call_args["quantity"] == 2  # Capped from 10 to 2
