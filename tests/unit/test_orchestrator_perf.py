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
        mock_get_broker.return_value.get_positions.return_value = []
        orch = Orchestrator()
        result = orch.get_performance_context()
        assert "PERFORMANCE CONTEXT" in result
        assert "Win rate" in result
        assert "Consecutive losses" in result

    @patch("agents.orchestrator.get_broker")
    def test_counts_consecutive_losses(self, mock_get_broker) -> None:
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
        mock_positions = [MagicMock(), MagicMock()]
        mock_get_broker.return_value.get_positions.return_value = mock_positions
        orch = Orchestrator()
        result = orch.get_performance_context()
        assert "Open positions: 2" in result
