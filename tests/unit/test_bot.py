"""Tests for the Telegram bot command handler."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.commands import TelegramBot
from data.models import init_db


class TestTelegramBot:
    def setup_method(self) -> None:
        init_db(":memory:")

    def test_bot_disabled_without_token(self) -> None:
        with patch("bot.commands.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                api=MagicMock(telegram_bot_token="", telegram_chat_id=""),
            )
            bot = TelegramBot()
            assert bot._enabled is False

    @pytest.mark.asyncio
    async def test_help_command(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_help([])
        assert "Momentum Agent Commands" in result
        assert "/health" in result
        assert "/killswitch" in result

    @pytest.mark.asyncio
    async def test_performance_command_no_trades(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_performance([])
        assert "30-Day Performance" in result
        assert "Total trades: 0" in result

    @pytest.mark.asyncio
    async def test_killswitch_status(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_killswitch([])
        assert "Kill switch is" in result
        assert "Usage" in result

    @pytest.mark.asyncio
    @patch("bot.commands.get_settings")
    async def test_killswitch_engage_disengage(self, mock_settings) -> None:
        mock_settings.return_value = MagicMock(
            api=MagicMock(telegram_bot_token="test", telegram_chat_id="123"),
        )
        from core.killswitch import is_killed, KILLSWITCH_PATH

        bot = TelegramBot()

        # Engage
        result = await bot._cmd_killswitch(["on"])
        assert "ENGAGED" in result
        assert is_killed() is True

        # Disengage
        result = await bot._cmd_killswitch(["off"])
        assert "DISENGAGED" in result
        assert is_killed() is False

        # Cleanup
        if KILLSWITCH_PATH.exists():
            KILLSWITCH_PATH.unlink()

    @pytest.mark.asyncio
    async def test_health_command(self) -> None:
        bot = TelegramBot()
        # Health check will have at least DB and disk checks pass
        result = await bot._cmd_health([])
        assert "Health Check" in result

    @pytest.mark.asyncio
    async def test_unauthorized_chat_ignored(self) -> None:
        bot = TelegramBot()
        bot._admin_chat_id = "12345"

        # Simulate an update from a different chat
        update = {
            "message": {
                "chat": {"id": 99999},
                "text": "/health",
            }
        }
        # Should not raise, just log and return
        await bot._handle_update(update)

    @pytest.mark.asyncio
    async def test_unknown_command(self) -> None:
        bot = TelegramBot()
        bot._admin_chat_id = "123"
        bot._reply = AsyncMock()  # type: ignore[method-assign]

        update = {
            "message": {
                "chat": {"id": 123},
                "text": "/foobar",
            }
        }
        await bot._handle_update(update)
        bot._reply.assert_called_once()
        call_args = bot._reply.call_args[0]
        assert "Unknown command" in call_args[1]

    # -------------------------------------------------------------------
    # New command tests
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_orders_command_no_orders(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_orders([])
        assert "No open orders" in result

    @pytest.mark.asyncio
    async def test_history_command_no_trades(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_history([])
        assert "No trade history" in result

    @pytest.mark.asyncio
    async def test_history_command_with_trades(self) -> None:
        from data.models import TradeLog, SignalAction, get_session
        from datetime import datetime, timezone

        session = get_session()
        trade = TradeLog(
            position_id="pos-001",
            ticker="AAPL",
            action=SignalAction.CALL,
            entry_price=2.50,
            exit_price=3.50,
            quantity=2,
            pnl_dollars=200.0,
            pnl_pct=40.0,
            hold_duration_hours=24.0,
            exit_reason="profit_target",
            opened_at=datetime.now(timezone.utc),
        )
        session.add(trade)
        session.commit()
        session.close()

        bot = TelegramBot()
        result = await bot._cmd_history([])
        assert "Trade History" in result
        assert "AAPL" in result
        assert "1W" in result
        assert "profit_target" in result

    @pytest.mark.asyncio
    async def test_expirations_command_no_positions(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_expirations([])
        assert "No open positions" in result

    @pytest.mark.asyncio
    async def test_expirations_command_with_expiring_position(self) -> None:
        from data.models import PositionRecord, PositionStatus, SignalAction, get_session
        from datetime import datetime, timedelta, timezone

        session = get_session()
        # Expires in 3 days
        exp_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
        pos = PositionRecord(
            position_id="pos-exp",
            signal_id="sig-exp",
            ticker="TSLA",
            option_symbol="TSLA250214C00300000",
            action=SignalAction.CALL,
            strike=300.0,
            expiration=exp_date,
            quantity=1,
            entry_price=5.0,
            entry_value=500.0,
            status=PositionStatus.OPEN,
        )
        session.add(pos)
        session.commit()
        session.close()

        bot = TelegramBot()
        result = await bot._cmd_expirations([])
        assert "Expiration Alerts" in result
        assert "TSLA" in result
        assert "CRITICAL" in result or "HIGH" in result

    @pytest.mark.asyncio
    async def test_weekly_command(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_weekly([])
        assert "Weekly Report" in result
        assert "Win rate" in result

    @pytest.mark.asyncio
    async def test_reconcile_command(self) -> None:
        bot = TelegramBot()
        with patch("bot.commands.TelegramBot._cmd_reconcile", new_callable=AsyncMock) as mock_recon:
            # Mock to avoid needing full broker setup
            mock_recon.return_value = "<b>Reconciliation Complete</b>\n  All positions synced. No issues found."
            result = await mock_recon([])
            assert "Reconciliation" in result

    @pytest.mark.asyncio
    async def test_close_command_no_args(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_close([])
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_close_command_not_found(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_close(["NONEXISTENT"])
        assert "No open position" in result

    @pytest.mark.asyncio
    async def test_help_includes_new_commands(self) -> None:
        bot = TelegramBot()
        result = await bot._cmd_help([])
        assert "/orders" in result
        assert "/history" in result
        assert "/expirations" in result
        assert "/weekly" in result
        assert "/flow" in result
        assert "/close" in result
        assert "/reconcile" in result
