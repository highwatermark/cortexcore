"""Tests for risk management tools."""
from __future__ import annotations

from unittest.mock import patch

from data.models import init_db
from tools.risk_tools import pre_trade_check


class TestPreTradeCheck:
    def test_approved_healthy_portfolio(self) -> None:
        signal = {
            "signal_id": "sig-1",
            "ticker": "AAPL",
            "iv_rank": 30,
            "dte": 30,
            "open_interest": 1000,
            "score": 8,
            "conviction": 85,
        }
        risk = {
            "risk_score": 20,
            "risk_level": "HEALTHY",
            "position_count": 2,
            "risk_capacity_pct": 0.80,
        }
        result = pre_trade_check(signal, risk)
        assert result["approved"] is True

    def test_denied_max_positions(self) -> None:
        signal = {
            "signal_id": "sig-2",
            "ticker": "TSLA",
            "iv_rank": 30,
            "dte": 30,
            "open_interest": 1000,
            "score": 8,
        }
        risk = {
            "risk_score": 20,
            "risk_level": "HEALTHY",
            "position_count": 3,
            "risk_capacity_pct": 0.80,
        }
        result = pre_trade_check(signal, risk)
        assert result["approved"] is False
        assert any("Max positions" in r for r in result["reasons"])

    def test_denied_critical_risk(self) -> None:
        signal = {
            "signal_id": "sig-3",
            "ticker": "NVDA",
            "iv_rank": 30,
            "dte": 30,
            "open_interest": 1000,
            "score": 8,
        }
        risk = {
            "risk_score": 80,
            "risk_level": "CRITICAL",
            "position_count": 3,
            "risk_capacity_pct": 0.05,
        }
        result = pre_trade_check(signal, risk)
        assert result["approved"] is False
        assert any("CRITICAL" in r for r in result["reasons"])

    def test_denied_high_iv(self) -> None:
        signal = {
            "signal_id": "sig-4",
            "ticker": "META",
            "iv_rank": 85,
            "dte": 30,
            "open_interest": 1000,
            "score": 8,
        }
        risk = {
            "risk_score": 20,
            "risk_level": "HEALTHY",
            "position_count": 1,
            "risk_capacity_pct": 0.80,
        }
        result = pre_trade_check(signal, risk)
        assert result["approved"] is False
        assert any("IV rank" in r for r in result["reasons"])

    def test_denied_low_dte(self) -> None:
        signal = {
            "signal_id": "sig-5",
            "ticker": "GOOG",
            "iv_rank": 30,
            "dte": 3,
            "open_interest": 1000,
            "score": 8,
        }
        risk = {
            "risk_score": 20,
            "risk_level": "HEALTHY",
            "position_count": 1,
            "risk_capacity_pct": 0.80,
        }
        result = pre_trade_check(signal, risk)
        assert result["approved"] is False
        assert any("DTE" in r for r in result["reasons"])

    def test_denied_low_score(self) -> None:
        signal = {
            "signal_id": "sig-6",
            "ticker": "AMZN",
            "iv_rank": 30,
            "dte": 30,
            "open_interest": 1000,
            "score": 4,
        }
        risk = {
            "risk_score": 20,
            "risk_level": "HEALTHY",
            "position_count": 1,
            "risk_capacity_pct": 0.80,
        }
        result = pre_trade_check(signal, risk)
        assert result["approved"] is False
        assert any("Score" in r for r in result["reasons"])

    def test_exceptional_conviction_overrides_low_capacity(self) -> None:
        signal = {
            "signal_id": "sig-7",
            "ticker": "AAPL",
            "iv_rank": 30,
            "dte": 30,
            "open_interest": 1000,
            "score": 9,
            "conviction": 95,
        }
        risk = {
            "risk_score": 55,
            "risk_level": "ELEVATED",
            "position_count": 3,
            "risk_capacity_pct": 0.10,
        }
        result = pre_trade_check(signal, risk)
        # Low capacity but exceptional conviction — still approved (capacity issue is noted but not blocking)
        assert any("exceptional" in r.lower() for r in result["reasons"])
