"""Tests for data models and database operations."""
from __future__ import annotations

from data.models import (
    FlowSignal,
    OrderSide,
    PositionRecord,
    PositionSnapshot,
    PositionStatus,
    RiskAssessment,
    RiskLevel,
    SignalAction,
    SignalRecord,
    TradeLog,
    TradeRequest,
    TradeResult,
    init_db,
    get_session,
)


class TestEnums:
    def test_signal_action_values(self) -> None:
        assert SignalAction.CALL == "CALL"
        assert SignalAction.PUT == "PUT"

    def test_position_status_values(self) -> None:
        assert PositionStatus.OPEN == "OPEN"
        assert PositionStatus.CLOSED == "CLOSED"

    def test_risk_level_values(self) -> None:
        assert RiskLevel.HEALTHY == "HEALTHY"
        assert RiskLevel.CRITICAL == "CRITICAL"


class TestPydanticSchemas:
    def test_flow_signal_defaults(self) -> None:
        sig = FlowSignal(
            ticker="AAPL",
            action=SignalAction.CALL,
            strike=175.0,
            expiration="2026-03-21",
            premium=250000,
            volume=500,
            open_interest=1000,
            vol_oi_ratio=0.5,
            option_type="CALL",
        )
        assert sig.ticker == "AAPL"
        assert sig.score == 0
        assert sig.signal_id  # auto-generated

    def test_position_snapshot(self) -> None:
        snap = PositionSnapshot(
            position_id="abc123",
            ticker="TSLA",
            option_symbol="TSLA260321C00250000",
            action=SignalAction.CALL,
            strike=250.0,
            expiration="2026-03-21",
            quantity=2,
            entry_price=5.50,
            current_price=7.20,
            pnl_pct=30.9,
            pnl_dollars=340.0,
        )
        assert snap.pnl_pct == 30.9
        assert snap.delta == 0.0  # default

    def test_risk_assessment_defaults(self) -> None:
        ra = RiskAssessment()
        assert ra.risk_score == 0
        assert ra.risk_level == RiskLevel.HEALTHY
        assert ra.can_add_position is True
        assert ra.warnings == []

    def test_trade_result(self) -> None:
        result = TradeResult(success=True, broker_order_id="ord-123")
        assert result.success
        assert result.error == ""

    def test_trade_request(self) -> None:
        req = TradeRequest(
            signal_id="sig-1",
            ticker="AAPL",
            option_symbol="AAPL260321C00175000",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=5.0,
        )
        assert req.conviction == 0  # default


class TestDatabase:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_create_signal_record(self) -> None:
        session = get_session()
        record = SignalRecord(
            signal_id="test-sig-1",
            ticker="AAPL",
            action=SignalAction.CALL,
            strike=175.0,
            expiration="2026-03-21",
            premium=250000,
            volume=500,
            open_interest=1000,
            vol_oi_ratio=0.5,
            option_type="CALL",
            score=8,
            accepted=True,
        )
        session.add(record)
        session.commit()

        fetched = session.query(SignalRecord).filter_by(signal_id="test-sig-1").first()
        assert fetched is not None
        assert fetched.ticker == "AAPL"
        assert fetched.score == 8
        assert fetched.accepted is True
        session.close()

    def test_create_position_record(self) -> None:
        session = get_session()
        pos = PositionRecord(
            position_id="pos-1",
            signal_id="sig-1",
            ticker="TSLA",
            option_symbol="TSLA260321C00250000",
            action=SignalAction.CALL,
            strike=250.0,
            expiration="2026-03-21",
            quantity=2,
            entry_price=5.50,
            entry_value=1100.0,
            status=PositionStatus.OPEN,
        )
        session.add(pos)
        session.commit()

        fetched = session.query(PositionRecord).filter_by(position_id="pos-1").first()
        assert fetched is not None
        assert fetched.status == PositionStatus.OPEN
        assert fetched.quantity == 2
        session.close()

    def test_trade_log_pnl(self) -> None:
        session = get_session()
        from datetime import datetime, timezone
        trade = TradeLog(
            position_id="pos-1",
            ticker="AAPL",
            action=SignalAction.CALL,
            entry_price=5.0,
            exit_price=7.5,
            quantity=1,
            pnl_dollars=250.0,
            pnl_pct=50.0,
            hold_duration_hours=24.0,
            exit_reason="profit_target",
            opened_at=datetime.now(timezone.utc),
        )
        session.add(trade)
        session.commit()

        fetched = session.query(TradeLog).first()
        assert fetched.pnl_dollars == 250.0
        assert fetched.exit_reason == "profit_target"
        session.close()
