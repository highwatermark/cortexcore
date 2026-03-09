"""Tests for execution tools — position sizing and exit failure handling."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.models import (
    IntentStatus,
    OrderIntent,
    PositionRecord,
    PositionStatus,
    SignalAction,
    TradeLog,
    init_db,
    get_session,
)
from tools.execution_tools import calculate_position_size, execute_exit


def _mock_broker_position(entry_price=3.0, quantity=1):
    """Mock a broker position with given entry price and quantity."""
    pos = MagicMock()
    pos.entry_price = entry_price
    pos.quantity = quantity
    pos.current_price = entry_price
    return pos


def _mock_broker_no_positions():
    """Mock AlpacaBroker returning no positions."""
    broker = MagicMock()
    broker.get_positions.return_value = []
    return broker


def _mock_broker_with_exposure(exposure_value: float, price_per_contract: float = 20.0):
    """Mock AlpacaBroker returning positions with given total exposure.

    exposure_value = entry_price * quantity * 100
    """
    broker = MagicMock()
    # Calculate quantity from exposure: exposure = price * qty * 100
    qty = max(1, int(exposure_value / (price_per_contract * 100)))
    actual_price = exposure_value / (qty * 100)
    pos = _mock_broker_position(entry_price=actual_price, quantity=qty)
    broker.get_positions.return_value = [pos]
    return broker


class TestCalculatePositionSize:
    """Test deterministic position sizing logic."""

    @patch("tools.execution_tools.AlpacaBroker")
    def test_basic_sizing_with_equity(self, mock_broker_cls):
        """With $10K equity, $2.50 option → cost $250/contract."""
        mock_broker_cls.return_value = _mock_broker_no_positions()

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

    @patch("tools.execution_tools.AlpacaBroker")
    def test_cheap_option_position_value_caps(self, mock_broker_cls):
        """Cheap option ($0.50 = $50/contract), position value $1K → 20 contracts max,
        but per_trade_pct or position_value_cap will limit."""
        mock_broker_cls.return_value = _mock_broker_no_positions()

        result = calculate_position_size(option_price=0.50, equity=10_000)

        # per_trade_pct: 10000 * 0.20 / 50 = 40
        # position_value_cap: 1000 / 50 = 20
        # total_exposure: 10000 * 0.25 / 50 = 50
        # min(40, 20, 50) = 20
        assert result["max_contracts"] == 20
        assert result["limiting_factor"] == "position_value_cap"

    @patch("tools.execution_tools.AlpacaBroker")
    def test_expensive_option_per_trade_limits(self, mock_broker_cls):
        """Expensive option ($5.00 = $500/contract)."""
        mock_broker_cls.return_value = _mock_broker_no_positions()

        result = calculate_position_size(option_price=5.00, equity=10_000)

        # per_trade_pct: 10000 * 0.20 / 500 = 4
        # position_value_cap: 1000 / 500 = 2
        # total_exposure: 10000 * 0.25 / 500 = 5
        # min(4, 2, 5) = 2
        assert result["max_contracts"] == 2
        assert result["limiting_factor"] == "position_value_cap"

    @patch("tools.execution_tools.AlpacaBroker")
    def test_existing_exposure_reduces_capacity(self, mock_broker_cls):
        """With existing $2K exposure out of $2.5K max (25% of $10K), only $500 left."""
        mock_broker_cls.return_value = _mock_broker_with_exposure(2000)

        result = calculate_position_size(option_price=2.50, equity=10_000)

        # per_trade_pct: 10000 * 0.20 / 250 = 8
        # position_value_cap: 1000 / 250 = 4
        # total_exposure: (10000 * 0.25 - 2000) / 250 = 500 / 250 = 2
        # min(8, 4, 2) = 2
        assert result["max_contracts"] == 2
        assert result["limiting_factor"] == "total_exposure"
        assert result["remaining_capacity"] == 500.0

    @patch("tools.execution_tools.AlpacaBroker")
    def test_no_capacity_remaining(self, mock_broker_cls):
        """Fully exposed — no room for new positions."""
        mock_broker_cls.return_value = _mock_broker_with_exposure(2500)

        result = calculate_position_size(option_price=2.50, equity=10_000)

        assert result["max_contracts"] == 0
        assert result["limiting_factor"] == "total_exposure"

    @patch("tools.execution_tools.AlpacaBroker")
    def test_over_exposed_returns_zero(self, mock_broker_cls):
        """If current exposure exceeds limit, returns 0."""
        mock_broker_cls.return_value = _mock_broker_with_exposure(5000)

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
    def test_fetches_equity_from_broker(self, mock_broker_cls):
        """When equity not provided, fetches from broker."""
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = {"equity": 10_000}
        mock_broker.get_positions.return_value = []
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
        mock_gate.assert_not_called()

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

        gate = MagicMock()
        gate.check_entry.return_value = (True, "")
        mock_gate.return_value = gate

        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        mock_get_session.return_value = session

        broker = MagicMock()
        order_result = MagicMock()
        order_result.success = True
        order_result.broker_order_id = "order-123"
        broker.submit_limit_order.return_value = order_result
        broker.get_order_status.return_value = {"status": "filled", "filled_qty": 2, "filled_avg_price": 2.50}
        mock_broker_cls.return_value = broker

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
            quantity=10,
            limit_price=2.50,
        ))

        call_args = gate.check_entry.call_args[0][0]
        assert call_args["quantity"] == 2


class TestExitFailureHandling:
    """Test exit fail counter, limit fallback, and auto-abandon."""

    def setup_method(self) -> None:
        init_db(":memory:")

    def _create_open_position(self, session, position_id="pos-fail-1",
                               exit_fail_count=0) -> PositionRecord:
        pos = PositionRecord(
            position_id=position_id,
            signal_id="sig-1",
            ticker="ARRY",
            option_symbol="ARRY260320C00012000",
            action=SignalAction.CALL,
            strike=12.0,
            expiration="2026-03-20",
            quantity=7,
            entry_price=0.65,
            entry_value=455.0,
            current_price=0.01,
            status=PositionStatus.OPEN,
            exit_fail_count=exit_fail_count,
            opened_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        session.add(pos)
        session.commit()
        return pos

    @pytest.mark.asyncio
    @patch("tools.execution_tools.TelegramNotifier")
    @patch("tools.execution_tools.AlpacaBroker")
    async def test_exit_fail_counter_increments(self, mock_broker_cls, mock_notifier_cls):
        """Exit fail counter increments when market order fails."""
        session = get_session()
        self._create_open_position(session, exit_fail_count=0)
        session.close()

        broker = MagicMock()
        market_result = MagicMock()
        market_result.success = False
        market_result.error = "no available quote for ARRY260320C00012000"
        broker.submit_market_order.return_value = market_result
        # Limit fallback also fails
        limit_result = MagicMock()
        limit_result.success = False
        limit_result.error = "no available quote"
        broker.submit_limit_order.return_value = limit_result
        mock_broker_cls.return_value = broker

        mock_notifier_cls.return_value = AsyncMock()

        result = await execute_exit("pos-fail-1", reason="stop_loss", use_market=True)

        assert result["success"] is False

        # Verify counter incremented in DB
        session = get_session()
        pos = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-fail-1"
        ).first()
        assert pos.exit_fail_count == 1
        assert pos.status == PositionStatus.OPEN
        session.close()

    @pytest.mark.asyncio
    @patch("tools.execution_tools.TelegramNotifier")
    @patch("tools.execution_tools.AlpacaBroker")
    async def test_auto_abandon_after_max_failures(self, mock_broker_cls, mock_notifier_cls):
        """Position auto-abandoned after reaching max_exit_failures."""
        session = get_session()
        # Set exit_fail_count to 9 (one below default threshold of 10)
        self._create_open_position(session, exit_fail_count=9)
        session.close()

        broker = MagicMock()
        market_result = MagicMock()
        market_result.success = False
        market_result.error = "no available quote"
        broker.submit_market_order.return_value = market_result
        limit_result = MagicMock()
        limit_result.success = False
        limit_result.error = "no available quote"
        broker.submit_limit_order.return_value = limit_result
        mock_broker_cls.return_value = broker

        notifier = AsyncMock()
        mock_notifier_cls.return_value = notifier

        result = await execute_exit("pos-fail-1", reason="stop_loss", use_market=True)

        assert result["success"] is False
        assert result.get("abandoned") is True

        # Verify DB state
        session = get_session()
        pos = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-fail-1"
        ).first()
        assert pos.status == PositionStatus.ABANDONED
        assert pos.exit_fail_count == 10
        assert pos.closed_at is not None

        # Verify TradeLog created with -100% loss
        trade = session.query(TradeLog).filter(
            TradeLog.position_id == "pos-fail-1"
        ).first()
        assert trade is not None
        assert trade.exit_price == 0.0
        assert trade.pnl_pct == -100.0
        assert "ABANDONED" in trade.exit_reason

        # Verify exit intent cleaned up
        intent = session.query(OrderIntent).filter(
            OrderIntent.idempotency_key == "exit-pos-fail-1"
        ).first()
        assert intent is None
        session.close()

        # Verify Telegram notified
        notifier.send.assert_called_once()

    @pytest.mark.asyncio
    @patch("tools.execution_tools.TelegramNotifier")
    @patch("tools.execution_tools.AlpacaBroker")
    async def test_limit_fallback_on_no_quote(self, mock_broker_cls, mock_notifier_cls):
        """When market order fails with 'no available quote', tries limit at $0.01."""
        session = get_session()
        self._create_open_position(session, exit_fail_count=0)
        session.close()

        broker = MagicMock()
        # Market order fails
        market_result = MagicMock()
        market_result.success = False
        market_result.error = "no available quote for ARRY260320C00012000"
        broker.submit_market_order.return_value = market_result

        # Limit fallback succeeds
        limit_result = MagicMock()
        limit_result.success = True
        limit_result.broker_order_id = "order-penny-123"
        broker.submit_limit_order.return_value = limit_result

        # Fill status
        broker.get_order_status.return_value = {
            "status": "filled",
            "filled_qty": 7,
            "filled_avg_price": 0.01,
        }
        mock_broker_cls.return_value = broker

        notifier = AsyncMock()
        mock_notifier_cls.return_value = notifier

        result = await execute_exit("pos-fail-1", reason="stop_loss", use_market=True)

        assert result["success"] is True
        # Verify limit order was submitted at $0.01
        broker.submit_limit_order.assert_called_once()
        call_kwargs = broker.submit_limit_order.call_args
        assert call_kwargs[1]["limit_price"] == 0.01
