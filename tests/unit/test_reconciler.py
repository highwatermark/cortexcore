"""Tests for position reconciliation and health checks."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.reconciler import reconcile_positions
from core.health import HealthChecker
from data.models import (
    PositionRecord,
    PositionStatus,
    PositionSnapshot,
    SignalAction,
    init_db,
    get_session,
)


def _make_broker_snapshot(**overrides) -> PositionSnapshot:
    defaults = dict(
        position_id="broker-pos-1",
        ticker="AAPL",
        option_symbol="AAPL260320C00200000",
        action=SignalAction.CALL,
        strike=200.0,
        expiration="2026-03-20",
        quantity=1,
        entry_price=3.50,
        current_price=4.00,
        pnl_pct=14.29,
        pnl_dollars=50.0,
        dte_remaining=30,
    )
    defaults.update(overrides)
    return PositionSnapshot(**defaults)


class TestReconcilePositions:
    def setup_method(self) -> None:
        init_db(":memory:")

    @pytest.mark.asyncio
    @patch("core.reconciler.TelegramNotifier")
    @patch("core.reconciler.get_broker")
    async def test_clean_reconciliation(self, mock_get_broker, mock_notifier_cls) -> None:
        """No orphans or phantoms when broker and DB match."""
        mock_broker = MagicMock()
        snapshot = _make_broker_snapshot()
        mock_broker.get_positions.return_value = [snapshot]
        mock_get_broker.return_value = mock_broker

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        # Add matching DB position
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-1", signal_id="sig-1", ticker="AAPL",
            option_symbol="AAPL260320C00200000", action=SignalAction.CALL,
            strike=200, expiration="2026-03-20", quantity=1,
            entry_price=3.50, entry_value=350, current_price=3.90,
            status=PositionStatus.OPEN,
        ))
        session.commit()
        session.close()

        result = await reconcile_positions()
        assert result["orphans_adopted"] == 0
        assert result["phantoms_closed"] == 0

    @pytest.mark.asyncio
    @patch("core.reconciler.TelegramNotifier")
    @patch("core.reconciler.get_broker")
    async def test_orphan_adopted(self, mock_get_broker, mock_notifier_cls) -> None:
        """Position in Alpaca but not in DB gets adopted."""
        mock_broker = MagicMock()
        snapshot = _make_broker_snapshot()
        mock_broker.get_positions.return_value = [snapshot]
        mock_get_broker.return_value = mock_broker

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        # No DB positions
        result = await reconcile_positions()
        assert result["orphans_adopted"] == 1

        # Verify it was added to DB
        session = get_session()
        count = session.query(PositionRecord).filter(
            PositionRecord.option_symbol == "AAPL260320C00200000"
        ).count()
        session.close()
        assert count == 1

    @pytest.mark.asyncio
    @patch("core.reconciler.TelegramNotifier")
    @patch("core.reconciler.get_broker")
    async def test_phantom_closed(self, mock_get_broker, mock_notifier_cls) -> None:
        """Position in DB but not in Alpaca gets marked CLOSED."""
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []  # Empty broker
        mock_get_broker.return_value = mock_broker

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        # Add DB position with no matching broker position
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-phantom", signal_id="sig-1", ticker="NVDA",
            option_symbol="NVDA260320C00500000", action=SignalAction.CALL,
            strike=500, expiration="2026-03-20", quantity=1,
            entry_price=5.0, entry_value=500, status=PositionStatus.OPEN,
        ))
        session.commit()
        session.close()

        result = await reconcile_positions()
        assert result["phantoms_closed"] == 1

        # Verify position is now CLOSED
        session = get_session()
        pos = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-phantom"
        ).first()
        session.close()
        assert pos.status == PositionStatus.CLOSED

    @pytest.mark.asyncio
    @patch("core.reconciler.TelegramNotifier")
    @patch("core.reconciler.get_broker")
    async def test_price_drift_corrected(self, mock_get_broker, mock_notifier_cls) -> None:
        """Large price drift between broker and DB gets corrected."""
        mock_broker = MagicMock()
        # Broker says price is 5.00
        snapshot = _make_broker_snapshot(current_price=5.00)
        mock_broker.get_positions.return_value = [snapshot]
        mock_get_broker.return_value = mock_broker

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        # DB says price is 3.50 (>10% drift)
        session = get_session()
        session.add(PositionRecord(
            position_id="pos-drift", signal_id="sig-1", ticker="AAPL",
            option_symbol="AAPL260320C00200000", action=SignalAction.CALL,
            strike=200, expiration="2026-03-20", quantity=1,
            entry_price=3.50, entry_value=350, current_price=3.50,
            status=PositionStatus.OPEN,
        ))
        session.commit()
        session.close()

        result = await reconcile_positions()
        assert result["drift_corrections"] == 1

        # Verify DB updated to broker price
        session = get_session()
        pos = session.query(PositionRecord).filter(
            PositionRecord.position_id == "pos-drift"
        ).first()
        session.close()
        assert pos.current_price == 5.00


class TestHealthChecker:
    def setup_method(self) -> None:
        init_db(":memory:")

    @pytest.mark.asyncio
    async def test_database_check_passes(self) -> None:
        checker = HealthChecker()
        result = checker._check_database()
        assert result.ok is True
        assert result.name == "database"

    @pytest.mark.asyncio
    async def test_disk_space_check_passes(self) -> None:
        checker = HealthChecker()
        result = checker._check_disk_space()
        assert result.ok is True
        assert "free" in result.detail

    @pytest.mark.asyncio
    async def test_to_dict(self) -> None:
        checker = HealthChecker()
        result = checker._check_database()
        d = result.to_dict()
        assert "name" in d
        assert "ok" in d
        assert "detail" in d
        assert "latency_ms" in d
