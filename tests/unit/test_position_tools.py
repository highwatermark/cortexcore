"""Tests for position management tools."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from data.models import (
    PositionRecord,
    PositionStatus,
    SignalAction,
    init_db,
    get_session,
)
from tools.position_tools import check_exit_triggers, get_open_positions, _get_adaptive_profit_target


class TestExitTriggers:
    def test_profit_target_trigger(self) -> None:
        position = {
            "position_id": "pos-1",
            "ticker": "AAPL",
            "pnl_pct": 55.0,
            "dte_remaining": 20,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert result["urgency"] == "high"
        assert any("PROFIT_TARGET" in t for t in result["triggers"])

    def test_stop_loss_trigger(self) -> None:
        position = {
            "position_id": "pos-2",
            "ticker": "TSLA",
            "pnl_pct": -55.0,
            "dte_remaining": 20,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert result["urgency"] == "critical"
        assert any("STOP_LOSS" in t for t in result["triggers"])

    def test_gamma_risk_trigger(self) -> None:
        position = {
            "position_id": "pos-3",
            "ticker": "NVDA",
            "pnl_pct": 5.0,
            "dte_remaining": 3,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert any("GAMMA_RISK" in t for t in result["triggers"])

    def test_conviction_drop_trigger(self) -> None:
        position = {
            "position_id": "pos-4",
            "ticker": "META",
            "pnl_pct": -10.0,
            "dte_remaining": 25,
            "conviction": 40,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert any("CONVICTION_DROP" in t for t in result["triggers"])

    def test_no_triggers_healthy(self) -> None:
        position = {
            "position_id": "pos-5",
            "ticker": "GOOG",
            "pnl_pct": 15.0,
            "dte_remaining": 25,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": -0.05,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is False
        assert result["recommended_action"] == "HOLD"

    def test_theta_acceleration_warning(self) -> None:
        position = {
            "position_id": "pos-6",
            "ticker": "AMZN",
            "pnl_pct": 10.0,
            "dte_remaining": 20,
            "conviction": 80,
            "entry_price": 1.0,
            "theta": -0.10,  # 10% daily decay
        }
        result = check_exit_triggers(position)
        assert any("THETA_ACCEL" in t for t in result["triggers"])


class TestAdaptiveProfitTargets:
    def test_target_dte_gt_14(self) -> None:
        assert _get_adaptive_profit_target(20) == 0.40

    def test_target_dte_7_to_14(self) -> None:
        assert _get_adaptive_profit_target(10) == 0.35

    def test_target_dte_3_to_7(self) -> None:
        assert _get_adaptive_profit_target(5) == 0.25

    def test_target_dte_lt_3(self) -> None:
        assert _get_adaptive_profit_target(2) == 0.15

    def test_adaptive_trigger_fires_at_lower_target(self) -> None:
        """DTE=5 position with +26% P&L should trigger (target=25%)."""
        position = {
            "position_id": "pos-adapt-1",
            "ticker": "AAPL",
            "pnl_pct": 26.0,
            "dte_remaining": 5,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert any("PROFIT_TARGET" in t for t in result["triggers"])

    def test_adaptive_no_trigger_at_higher_dte(self) -> None:
        """DTE=20 position with +38% P&L should NOT trigger (target=40%)."""
        position = {
            "position_id": "pos-adapt-2",
            "ticker": "AAPL",
            "pnl_pct": 38.0,
            "dte_remaining": 20,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert not any("PROFIT_TARGET" in t for t in result["triggers"])


class TestMandatoryDteExit:
    def test_dte_4_triggers_mandatory_exit(self) -> None:
        position = {
            "position_id": "pos-dte-1",
            "ticker": "NVDA",
            "pnl_pct": 10.0,
            "dte_remaining": 4,
            "conviction": 90,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert result["urgency"] == "critical"
        assert any("DTE_MANDATORY" in t for t in result["triggers"])

    def test_dte_5_triggers_mandatory_exit(self) -> None:
        """DTE exactly at max_hold_dte should still trigger."""
        position = {
            "position_id": "pos-dte-2",
            "ticker": "NVDA",
            "pnl_pct": -20.0,  # Losing position — still must exit
            "dte_remaining": 5,
            "conviction": 90,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert result["should_exit"] is True
        assert any("DTE_MANDATORY" in t for t in result["triggers"])

    def test_dte_6_no_mandatory_exit(self) -> None:
        position = {
            "position_id": "pos-dte-3",
            "ticker": "NVDA",
            "pnl_pct": 10.0,
            "dte_remaining": 6,
            "conviction": 80,
            "entry_price": 5.0,
            "theta": 0.0,
        }
        result = check_exit_triggers(position)
        assert not any("DTE_MANDATORY" in t for t in result["triggers"])


class TestAbandonedPositionFiltering:
    """Test that get_open_positions excludes ABANDONED positions."""

    def setup_method(self) -> None:
        init_db(":memory:")

    @patch("tools.position_tools.AlpacaBroker")
    def test_abandoned_positions_excluded(self, mock_broker_cls) -> None:
        """Broker positions with ABANDONED DB record are filtered out."""
        # Create broker mock with two positions
        bp_active = MagicMock()
        bp_active.position_id = "bp-1"
        bp_active.ticker = "AAPL"
        bp_active.option_symbol = "AAPL260320C00200000"
        bp_active.action = SignalAction.CALL
        bp_active.strike = 200.0
        bp_active.expiration = "2026-03-20"
        bp_active.quantity = 1
        bp_active.entry_price = 3.50
        bp_active.current_price = 4.00
        bp_active.pnl_pct = 14.29
        bp_active.pnl_dollars = 50.0
        bp_active.dte_remaining = 30

        bp_abandoned = MagicMock()
        bp_abandoned.position_id = "bp-2"
        bp_abandoned.ticker = "ARRY"
        bp_abandoned.option_symbol = "ARRY260320C00012000"
        bp_abandoned.action = SignalAction.CALL
        bp_abandoned.strike = 12.0
        bp_abandoned.expiration = "2026-03-20"
        bp_abandoned.quantity = 7
        bp_abandoned.entry_price = 0.65
        bp_abandoned.current_price = 0.0
        bp_abandoned.pnl_pct = -100.0
        bp_abandoned.pnl_dollars = -455.0
        bp_abandoned.dte_remaining = 12

        broker = MagicMock()
        broker.get_positions.return_value = [bp_active, bp_abandoned]
        mock_broker_cls.return_value = broker

        # Create DB records: one OPEN, one ABANDONED
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-1", signal_id="sig-1", ticker="AAPL",
            option_symbol="AAPL260320C00200000", action=SignalAction.CALL,
            strike=200, expiration="2026-03-20", quantity=1,
            entry_price=3.50, entry_value=350, status=PositionStatus.OPEN,
        ))
        session.add(PositionRecord(
            position_id="pos-2", signal_id="sig-2", ticker="ARRY",
            option_symbol="ARRY260320C00012000", action=SignalAction.CALL,
            strike=12, expiration="2026-03-20", quantity=7,
            entry_price=0.65, entry_value=455, status=PositionStatus.ABANDONED,
        ))
        session.commit()
        session.close()

        results = get_open_positions()

        assert len(results) == 1
        assert results[0]["ticker"] == "AAPL"
        # ARRY should be filtered out
        assert not any(r["ticker"] == "ARRY" for r in results)


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
