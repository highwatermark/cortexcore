"""Tests for flow scanning tools."""
from __future__ import annotations

from data.models import init_db
from tools.flow_tools import score_signal, save_signal


class TestScoreSignal:
    def test_sweep_order_bonus(self) -> None:
        signal = {
            "signal_id": "test-1",
            "ticker": "AAPL",
            "order_type": "sweep",
            "vol_oi_ratio": 1.0,
            "premium": 100000,
            "iv_rank": 30,
            "dte": 30,
        }
        result = score_signal(signal)
        assert result["score"] >= 2  # sweep gives +2
        assert "sweep:+2" in result["breakdown"]

    def test_floor_trade_bonus(self) -> None:
        signal = {
            "signal_id": "test-2",
            "ticker": "MSFT",
            "order_type": "floor",
            "vol_oi_ratio": 2.0,
            "premium": 300000,
            "iv_rank": 30,
            "dte": 30,
        }
        result = score_signal(signal)
        assert "floor:+2" in result["breakdown"]

    def test_high_vol_oi_bonus(self) -> None:
        signal = {
            "signal_id": "test-3",
            "ticker": "NVDA",
            "order_type": "regular",
            "vol_oi_ratio": 3.5,
            "premium": 100000,
            "iv_rank": 30,
            "dte": 30,
        }
        result = score_signal(signal)
        assert "vol_oi>=3.0:+1" in result["breakdown"]

    def test_high_premium_bonus(self) -> None:
        signal = {
            "signal_id": "test-4",
            "ticker": "AMZN",
            "order_type": "regular",
            "vol_oi_ratio": 1.0,
            "premium": 600000,
            "iv_rank": 30,
            "dte": 30,
        }
        result = score_signal(signal)
        assert "premium>=500K:+2" in result["breakdown"]

    def test_iv_rank_penalty(self) -> None:
        signal = {
            "signal_id": "test-5",
            "ticker": "META",
            "order_type": "regular",
            "vol_oi_ratio": 1.0,
            "premium": 100000,
            "iv_rank": 85,
            "dte": 30,
        }
        result = score_signal(signal)
        assert result["score"] == 0  # penalty clamps to 0
        assert "iv_rank" in result["breakdown"]

    def test_low_dte_penalty(self) -> None:
        signal = {
            "signal_id": "test-6",
            "ticker": "GOOG",
            "order_type": "regular",
            "vol_oi_ratio": 1.0,
            "premium": 100000,
            "iv_rank": 30,
            "dte": 5,
        }
        result = score_signal(signal)
        assert "dte<7:-2" in result["breakdown"]

    def test_high_score_passes(self) -> None:
        signal = {
            "signal_id": "test-7",
            "ticker": "AAPL",
            "order_type": "sweep open",
            "vol_oi_ratio": 3.5,
            "premium": 600000,
            "iv_rank": 25,
            "dte": 30,
        }
        result = score_signal(signal)
        assert result["passed"] is True
        assert result["score"] >= 7

    def test_score_clamped_to_10(self) -> None:
        signal = {
            "signal_id": "test-8",
            "ticker": "AAPL",
            "order_type": "sweep floor open",
            "vol_oi_ratio": 5.0,
            "premium": 1000000,
            "iv_rank": 10,
            "dte": 30,
        }
        result = score_signal(signal)
        assert result["score"] <= 10


class TestSaveSignal:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_save_and_retrieve(self) -> None:
        signal = {
            "signal_id": "save-test-1",
            "ticker": "AAPL",
            "action": "CALL",
            "strike": 175.0,
            "expiration": "2026-03-21",
            "premium": 300000,
            "volume": 500,
            "open_interest": 1000,
            "vol_oi_ratio": 0.5,
            "option_type": "CALL",
            "order_type": "sweep",
            "underlying_price": 175.0,
            "iv_rank": 30,
            "dte": 30,
        }
        score_result = {"score": 8, "breakdown": "sweep:+2, vol_oi>=1.5:+1", "passed": True, "min_required": 7}

        signal_id = save_signal(signal, score_result)
        assert signal_id == "save-test-1"
