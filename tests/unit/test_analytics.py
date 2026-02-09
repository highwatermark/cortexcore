"""Tests for the performance analytics module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analytics.performance import (
    get_avg_hold_hours,
    get_daily_pnl,
    get_max_drawdown,
    get_performance_summary,
    get_profit_factor,
    get_sharpe_ratio,
    get_win_rate,
)
from data.models import SignalAction, TradeLog, get_session, init_db


def _add_trades(trades_data: list[dict]) -> None:
    """Helper to insert test trades."""
    session = get_session()
    now = datetime.now(timezone.utc)
    for i, td in enumerate(trades_data):
        session.add(TradeLog(
            position_id=f"pos-{i}",
            ticker=td.get("ticker", "AAPL"),
            action=SignalAction.CALL,
            entry_price=td["entry"],
            exit_price=td["exit"],
            quantity=td.get("qty", 1),
            pnl_dollars=td["pnl"],
            pnl_pct=td.get("pnl_pct", 0),
            hold_duration_hours=td.get("hold_hours", 8),
            opened_at=now - timedelta(days=td.get("days_ago", i)),
            closed_at=now - timedelta(days=td.get("days_ago", i)) + timedelta(hours=8),
        ))
    session.commit()
    session.close()


class TestWinRate:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_no_trades(self) -> None:
        assert get_win_rate() == 0.0

    def test_all_winners(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 200},
            {"entry": 4.0, "exit": 6.0, "pnl": 200},
        ])
        assert get_win_rate() == 1.0

    def test_mixed_results(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 200},
            {"entry": 3.0, "exit": 5.0, "pnl": 200},
            {"entry": 3.0, "exit": 5.0, "pnl": 200},
            {"entry": 5.0, "exit": 3.0, "pnl": -200},
            {"entry": 5.0, "exit": 3.0, "pnl": -200},
        ])
        assert get_win_rate() == 0.6


class TestProfitFactor:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_no_trades(self) -> None:
        assert get_profit_factor() == 0.0

    def test_all_wins_infinite(self) -> None:
        _add_trades([{"entry": 3.0, "exit": 5.0, "pnl": 200}])
        assert get_profit_factor() == float("inf")

    def test_mixed(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 600},
            {"entry": 5.0, "exit": 3.0, "pnl": -200},
        ])
        # 600 / 200 = 3.0
        assert get_profit_factor() == 3.0


class TestMaxDrawdown:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_no_trades(self) -> None:
        assert get_max_drawdown() == 0.0

    def test_steady_wins(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 200, "days_ago": 3},
            {"entry": 3.0, "exit": 5.0, "pnl": 200, "days_ago": 2},
        ])
        assert get_max_drawdown() == 0.0  # No drawdown in all wins

    def test_drawdown_calculated(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 1000, "days_ago": 3},
            {"entry": 5.0, "exit": 3.0, "pnl": -500, "days_ago": 2},
        ])
        # Peak = 1000, trough = 500, DD = 500/1000 = 0.50
        assert get_max_drawdown() == 0.50


class TestSharpeRatio:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_no_trades(self) -> None:
        assert get_sharpe_ratio() == 0.0

    def test_one_trade(self) -> None:
        _add_trades([{"entry": 3.0, "exit": 5.0, "pnl": 200}])
        assert get_sharpe_ratio() == 0.0  # Need 2+ days


class TestAvgHoldHours:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_no_trades(self) -> None:
        assert get_avg_hold_hours() == 0.0

    def test_average(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 200, "hold_hours": 10},
            {"entry": 3.0, "exit": 5.0, "pnl": 200, "hold_hours": 20},
        ])
        assert get_avg_hold_hours() == 15.0


class TestPerformanceSummary:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_summary_structure(self) -> None:
        _add_trades([
            {"entry": 3.0, "exit": 5.0, "pnl": 200, "days_ago": 2},
            {"entry": 5.0, "exit": 3.0, "pnl": -100, "days_ago": 1},
        ])
        summary = get_performance_summary()
        assert "total_trades" in summary
        assert summary["total_trades"] == 2
        assert "win_rate" in summary
        assert "profit_factor" in summary
        assert "max_drawdown" in summary
        assert "sharpe_ratio" in summary
        assert "avg_hold_hours" in summary
        assert "daily_pnl" in summary
        assert summary["total_pnl"] == 100.0
