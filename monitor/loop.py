"""
Main monitoring loop.

Runs the orchestrator on a configurable interval with:
- Market hours awareness (9:30-16:00 ET, uses Alpaca calendar for holidays)
- Adaptive scan frequency
- Position monitoring between scans
- Order fill reconciliation
- Daily summary at market close
- Circuit breaker on consecutive errors
- Graceful shutdown
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime

import pytz

from agents.orchestrator import Orchestrator
from bot.commands import TelegramBot
from config.settings import get_settings
from core.circuit_breaker import get_trading_breaker
from core.health import get_health_checker
from core.killswitch import is_killed
from core.logger import bind_cycle_id, get_logger
from data.models import PositionRecord, PositionStatus, TradeLog, get_session
from services.alpaca_broker import get_broker
from services.telegram import TelegramNotifier
from core.reconciler import reconcile_positions
from tools.execution_tools import reconcile_orders

log = get_logger("monitor")


class MonitorLoop:
    """Async monitoring loop that drives the trading system."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.orchestrator = Orchestrator()
        self.notifier = TelegramNotifier()
        self._broker = get_broker()
        self._running = False
        self._consecutive_errors = 0
        self._circuit_open = False
        self._cycle_count = 0
        self._market_open_today: bool | None = None
        self._last_calendar_check: str = ""
        self._daily_summary_sent = False
        self._health_checker = get_health_checker()
        self._last_health_check: datetime | None = None
        self._telegram_bot = TelegramBot()
        self._bot_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the monitoring loop. Blocks until shutdown."""
        self._running = True
        log.info("monitor_start", paper=self.settings.paper_trading, shadow=self.settings.shadow_mode)

        await self.notifier.send(
            f"<b>Momentum Agent Started</b>\n"
            f"Mode: {'PAPER' if self.settings.paper_trading else 'LIVE'}\n"
            f"Shadow: {'ON' if self.settings.shadow_mode else 'OFF'}"
        )

        # Start Telegram bot as background task
        self._bot_task = asyncio.create_task(self._telegram_bot.start())

        # Set up graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        try:
            while self._running:
                await self._tick()
        except asyncio.CancelledError:
            log.info("monitor_cancelled")
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Signal the loop to stop."""
        log.info("monitor_stop_requested")
        self._running = False

    async def _tick(self) -> None:
        """Single monitoring tick."""
        # Kill switch — highest priority
        if is_killed():
            if not getattr(self, "_kill_notified", False):
                log.critical("killswitch_active")
                await self.notifier.notify_error("KILL SWITCH", "Trading halted by kill switch. Remove KILLSWITCH file to resume.")
                self._kill_notified = True
            await asyncio.sleep(30)
            return
        self._kill_notified = False

        # Periodic health check (every 30 min)
        now_utc = datetime.now(pytz.UTC)
        if self._last_health_check is None or (now_utc - self._last_health_check).total_seconds() > 1800:
            try:
                results = await self._health_checker.run_all()
                self._last_health_check = now_utc
                if self._health_checker.consecutive_failures >= 3:
                    await self.notifier.notify_error(
                        "Health Check",
                        "3+ consecutive health check failures:\n" +
                        "\n".join(f"- {r.name}: {r.detail}" for r in results if not r.ok),
                    )
            except Exception as e:
                log.error("health_check_error", error=str(e))

        # Check market hours (with holiday awareness)
        if not self._is_market_open():
            # Check if we just passed market close and haven't sent summary
            if self._should_send_daily_summary():
                await self._send_daily_summary()
            log.debug("market_closed", next_check="60s")
            await asyncio.sleep(60)
            return

        # Reset daily summary flag when market opens
        self._daily_summary_sent = False

        # Trading circuit breaker (loss-based)
        try:
            account = self._broker.get_account()
            equity = account.get("equity", 100_000)
            breaker = get_trading_breaker()
            breaker_state = breaker.check(equity)
            if breaker_state.is_tripped:
                if not getattr(self, "_loss_breaker_notified", False):
                    await self.notifier.notify_error(
                        "Trading Circuit Breaker",
                        f"{breaker_state.reason}\nResumes: {breaker_state.resumes_at}",
                    )
                    self._loss_breaker_notified = True
                log.warning("trading_breaker_active", reason=breaker_state.reason)
                # Still do position checks (monitor existing positions), but skip scanning
                await reconcile_orders()
                log.info("cycle_position_check_only", cycle=self._cycle_count, reason="trading_breaker")
                result = await self.orchestrator.run_position_check()
                await asyncio.sleep(self.settings.monitor.poll_interval_seconds)
                return
            self._loss_breaker_notified = False
        except Exception as e:
            log.error("breaker_check_error", error=str(e))

        # Error circuit breaker
        if self._circuit_open:
            log.warning("circuit_breaker_open", cooldown=self.settings.monitor.circuit_breaker_cooldown_seconds)
            await asyncio.sleep(self.settings.monitor.circuit_breaker_cooldown_seconds)
            self._circuit_open = False
            self._consecutive_errors = 0
            log.info("circuit_breaker_reset")
            return

        self._cycle_count += 1
        bind_cycle_id(self._cycle_count)
        interval = self._get_scan_interval()

        try:
            # Reconcile any pending orders each cycle
            await reconcile_orders()

            # Position reconciliation every 5th cycle
            if self._cycle_count % 5 == 0:
                try:
                    recon = await reconcile_positions()
                    log.info("reconciliation_done", cycle=self._cycle_count, result=recon)
                except Exception as e:
                    log.error("reconciliation_failed", error=str(e))

            # Alternate between full scan and position check
            if self._cycle_count % 3 == 0:
                log.info("cycle_full_scan", cycle=self._cycle_count)
                result = await self.orchestrator.run_scan_cycle()
            else:
                log.info("cycle_position_check", cycle=self._cycle_count)
                result = await self.orchestrator.run_position_check()

            log.info("cycle_complete", cycle=self._cycle_count, result_preview=result[:200] if result else "")
            self._consecutive_errors = 0

        except Exception as e:
            self._consecutive_errors += 1
            log.error(
                "cycle_error",
                cycle=self._cycle_count,
                error=str(e),
                consecutive=self._consecutive_errors,
            )

            if self._consecutive_errors >= self.settings.monitor.max_consecutive_errors:
                self._circuit_open = True
                log.error("circuit_breaker_tripped", errors=self._consecutive_errors)
                await self.notifier.notify_error(
                    "Circuit breaker",
                    f"Tripped after {self._consecutive_errors} consecutive errors. Last: {str(e)[:200]}",
                )

        await asyncio.sleep(interval)

    def _is_market_open(self) -> bool:
        """Check if we're within market trading hours (with holiday awareness)."""
        mh = self.settings.market_hours
        tz = pytz.timezone(mh.timezone)
        now = datetime.now(tz)

        # Skip weekends
        if now.weekday() >= 5:
            return False

        # Check Alpaca calendar for holidays (cache per day)
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self._last_calendar_check:
            self._market_open_today = self._broker.is_market_open_today()
            self._last_calendar_check = today_str
            if not self._market_open_today:
                log.info("market_holiday", date=today_str)

        if not self._market_open_today:
            return False

        market_open = now.replace(hour=mh.open_hour, minute=mh.open_minute, second=0, microsecond=0)
        market_close = now.replace(hour=mh.close_hour, minute=mh.close_minute, second=0, microsecond=0)

        return market_open <= now <= market_close

    def _should_send_daily_summary(self) -> bool:
        """Check if we should send a daily summary (just after market close)."""
        if self._daily_summary_sent:
            return False

        mh = self.settings.market_hours
        tz = pytz.timezone(mh.timezone)
        now = datetime.now(tz)

        # Within 30 minutes after close
        market_close = now.replace(hour=mh.close_hour, minute=mh.close_minute, second=0, microsecond=0)
        minutes_past_close = (now - market_close).total_seconds() / 60

        return 0 < minutes_past_close < 30 and now.weekday() < 5

    async def _send_daily_summary(self) -> None:
        """Send enhanced end-of-day summary via Telegram."""
        self._daily_summary_sent = True
        session = get_session()
        try:
            from analytics.performance import get_performance_summary

            today = datetime.now().strftime("%Y-%m-%d")

            # Count trades today
            trades_today = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= today)
                .count()
            )

            # Total P&L today
            trades = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= today)
                .all()
            )
            total_pnl = sum(t.pnl_dollars for t in trades)

            # Open positions
            open_count = (
                session.query(PositionRecord)
                .filter(PositionRecord.status == PositionStatus.OPEN)
                .count()
            )

            # 30-day performance
            perf = get_performance_summary(30)

            # Account equity
            try:
                account = self._broker.get_account()
                equity = account.get("equity", 0)
            except Exception:
                equity = 0

            # Circuit breaker state
            from core.circuit_breaker import get_trading_breaker
            breaker = get_trading_breaker()
            breaker_state = breaker.check(equity) if equity else None

            # Enhanced Telegram message
            msg_parts = [
                f"<b>Daily Summary — {today}</b>",
                f"",
                f"<b>Today:</b>",
                f"  Trades: {trades_today}",
                f"  P&L: ${total_pnl:+,.2f}",
                f"  Open positions: {open_count}",
                f"",
                f"<b>Account:</b>",
                f"  Equity: ${equity:,.0f}",
                f"",
                f"<b>30-Day Performance:</b>",
                f"  Win rate: {perf['win_rate']:.0%}",
                f"  Profit factor: {perf['profit_factor']}",
                f"  Max drawdown: {perf['max_drawdown']:.1%}",
                f"  Sharpe ratio: {perf['sharpe_ratio']}",
                f"  Avg hold: {perf['avg_hold_hours']}h",
                f"  Total trades: {perf['total_trades']}",
                f"  Total P&L: ${perf['total_pnl']:+,.2f}",
            ]

            if breaker_state and breaker_state.is_tripped:
                msg_parts.append(f"\n<b>Circuit Breaker: TRIPPED</b>\n  {breaker_state.reason}")

            msg_parts.append(f"\nCycles today: {self._cycle_count}")

            await self.notifier.send("\n".join(msg_parts))
            log.info("daily_summary_sent", pnl=total_pnl, trades=trades_today, positions=open_count)
        except Exception as e:
            log.error("daily_summary_error", error=str(e))
        finally:
            session.close()

    def _get_scan_interval(self) -> int:
        """Get adaptive scan interval based on time of day."""
        mh = self.settings.market_hours
        flow = self.settings.flow
        tz = pytz.timezone(mh.timezone)
        now = datetime.now(tz)

        market_open = now.replace(hour=mh.open_hour, minute=mh.open_minute, second=0)
        market_close = now.replace(hour=mh.close_hour, minute=mh.close_minute, second=0)

        minutes_since_open = (now - market_open).total_seconds() / 60
        minutes_to_close = (market_close - now).total_seconds() / 60

        if minutes_since_open < 30 or minutes_to_close < 30:
            return flow.adaptive_scan_min_interval
        else:
            return self.settings.monitor.poll_interval_seconds

    async def _shutdown(self) -> None:
        """Clean shutdown."""
        log.info("monitor_shutdown")
        self._telegram_bot.stop()
        if self._bot_task and not self._bot_task.done():
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
        await self.notifier.send("<b>Momentum Agent Stopped</b>")
