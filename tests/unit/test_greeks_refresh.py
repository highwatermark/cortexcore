"""Tests for Greeks refresh, P&L persistence, and position monitoring."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from data.models import (
    PositionRecord,
    PositionStatus,
    SignalAction,
    init_db,
    get_session,
)
from tools.position_tools import update_position_greeks, refresh_positions


class TestUpdatePositionGreeks:
    def setup_method(self) -> None:
        init_db(":memory:")
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-greeks-1",
            signal_id="sig-1",
            ticker="AAPL",
            option_symbol="AAPL260320C00200000",
            action=SignalAction.CALL,
            strike=200.0,
            expiration="2026-03-20",
            quantity=2,
            entry_price=3.50,
            entry_value=700.0,
            status=PositionStatus.OPEN,
        ))
        session.commit()
        session.close()

    def test_updates_all_greeks(self) -> None:
        result = update_position_greeks(
            "pos-greeks-1",
            delta=0.55, gamma=0.08, theta=-0.12, vega=0.15, iv=0.32,
        )
        assert result is True

        session = get_session()
        pos = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-greeks-1"
        ).first()
        session.close()

        assert pos.delta == 0.55
        assert pos.gamma == 0.08
        assert pos.theta == -0.12
        assert pos.vega == 0.15
        assert pos.iv == 0.32
        assert pos.last_checked is not None

    def test_partial_update(self) -> None:
        result = update_position_greeks("pos-greeks-1", delta=0.60)
        assert result is True

        session = get_session()
        pos = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-greeks-1"
        ).first()
        session.close()

        assert pos.delta == 0.60
        assert pos.gamma is None  # Not updated

    def test_nonexistent_position_returns_false(self) -> None:
        result = update_position_greeks("pos-does-not-exist", delta=0.5)
        assert result is False


class TestRefreshPositions:
    def setup_method(self) -> None:
        init_db(":memory:")
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-refresh-1",
            signal_id="sig-1",
            ticker="AAPL",
            option_symbol="AAPL260320C00200000",
            action=SignalAction.CALL,
            strike=200.0,
            expiration="2026-03-20",
            quantity=2,
            entry_price=3.50,
            entry_value=700.0,
            status=PositionStatus.OPEN,
        ))
        session.add(PositionRecord(
            position_id="pos-refresh-2",
            signal_id="sig-2",
            ticker="NVDA",
            option_symbol="NVDA260320C00500000",
            action=SignalAction.CALL,
            strike=500.0,
            expiration="2026-03-20",
            quantity=1,
            entry_price=5.00,
            entry_value=500.0,
            status=PositionStatus.OPEN,
        ))
        session.commit()
        session.close()

    @patch("tools.position_tools.get_options_data_client")
    def test_updates_price_pnl_and_greeks(self, mock_get_client) -> None:
        mock_client = MagicMock()
        mock_client.get_snapshots.return_value = {
            "AAPL260320C00200000": {
                "current_price": 4.50,
                "delta": 0.65,
                "gamma": 0.05,
                "theta": -0.10,
                "vega": 0.12,
                "iv": 0.35,
            },
            "NVDA260320C00500000": {
                "current_price": 6.00,
                "delta": 0.70,
                "gamma": 0.04,
                "theta": -0.15,
                "vega": 0.20,
                "iv": 0.40,
            },
        }
        mock_get_client.return_value = mock_client

        result = refresh_positions()
        assert result["positions"] == 2
        assert result["updated"] == 2
        assert result["errors"] == 0

        session = get_session()

        # Check AAPL position
        aapl = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-refresh-1"
        ).first()
        assert aapl.current_price == 4.50
        assert aapl.current_value == 4.50 * 2 * 100  # 900.0
        assert aapl.pnl_pct == pytest.approx(((4.50 - 3.50) / 3.50) * 100, abs=0.01)
        assert aapl.pnl_dollars == pytest.approx((4.50 - 3.50) * 2 * 100, abs=0.01)
        assert aapl.delta == 0.65
        assert aapl.gamma == 0.05
        assert aapl.theta == -0.10
        assert aapl.vega == 0.12
        assert aapl.iv == 0.35

        # Check NVDA position
        nvda = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-refresh-2"
        ).first()
        assert nvda.current_price == 6.00
        assert nvda.delta == 0.70
        assert nvda.pnl_pct == pytest.approx(((6.00 - 5.00) / 5.00) * 100, abs=0.01)
        assert nvda.pnl_dollars == pytest.approx((6.00 - 5.00) * 1 * 100, abs=0.01)

        session.close()

    @patch("tools.position_tools.get_options_data_client")
    def test_handles_empty_snapshots(self, mock_get_client) -> None:
        """API returns no data — no positions updated, no errors."""
        mock_client = MagicMock()
        mock_client.get_snapshots.return_value = {}
        mock_get_client.return_value = mock_client

        result = refresh_positions()
        assert result["positions"] == 2
        assert result["updated"] == 0
        assert result["errors"] == 0

    @patch("tools.position_tools.get_options_data_client")
    def test_handles_api_failure(self, mock_get_client) -> None:
        """API call raises exception — returns gracefully."""
        mock_client = MagicMock()
        mock_client.get_snapshots.side_effect = Exception("API timeout")
        mock_get_client.return_value = mock_client

        result = refresh_positions()
        assert result["updated"] == 0
        assert "error" in result

    @patch("tools.position_tools.get_options_data_client")
    def test_partial_snapshot_data(self, mock_get_client) -> None:
        """Only one of two positions has snapshot data."""
        mock_client = MagicMock()
        mock_client.get_snapshots.return_value = {
            "AAPL260320C00200000": {
                "current_price": 4.00,
                "delta": 0.55,
                "gamma": None,
                "theta": -0.08,
                "vega": None,
                "iv": 0.30,
            },
        }
        mock_get_client.return_value = mock_client

        result = refresh_positions()
        assert result["positions"] == 2
        assert result["updated"] == 1

        session = get_session()
        aapl = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-refresh-1"
        ).first()
        assert aapl.delta == 0.55
        assert aapl.gamma is None  # None values are not written
        assert aapl.theta == -0.08

        # NVDA should be untouched
        nvda = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-refresh-2"
        ).first()
        assert nvda.current_price is None  # Never updated
        assert nvda.delta is None
        session.close()

    def test_no_open_positions(self) -> None:
        """No open positions returns early."""
        init_db(":memory:")  # Reset to empty DB
        result = refresh_positions()
        assert result["positions"] == 0
        assert result["updated"] == 0
