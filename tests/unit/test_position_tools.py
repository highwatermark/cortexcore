"""Tests for position management tools."""
from __future__ import annotations

from tools.position_tools import check_exit_triggers, _get_adaptive_profit_target


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
