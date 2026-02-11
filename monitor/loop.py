"""
Main monitoring loop — deterministic pipeline + Claude for decisions only.

Every cycle:
  1. Deterministic: scan flow, score, save, risk, positions, exit triggers,
     pre-trade checks, Telegram report — ZERO Claude API calls
  2. IF signals pass all checks → Claude called ONCE for entry decisions
  3. IF exit triggers fire → Claude called ONCE for exit decisions

Claude API calls per cycle: 0 (most cycles), 1-2 (when action needed)
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
from tools.flow_tools import scan_flow, score_signal, save_signal, send_scan_report
from tools.position_tools import get_open_positions, check_exit_triggers, refresh_positions
from tools.risk_tools import calculate_portfolio_risk, pre_trade_check

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
        self._last_greeks_refresh: datetime | None = None

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
                # Still monitor positions deterministically
                await reconcile_orders()
                await self._deterministic_position_check()
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

            # =========================================================
            # DETERMINISTIC PIPELINE — zero Claude API calls
            # =========================================================
            log.info("cycle_start", cycle=self._cycle_count)

            # Step 1: Scan flow (UW API call)
            signals = await scan_flow()
            log.info("pipeline_scan", signals=len(signals))

            # Step 2: Score each signal
            scored: list[tuple[dict, dict]] = []
            for sig in signals:
                result = score_signal(sig)
                save_signal(sig, result)
                scored.append((sig, result))

            # Step 3: Send Telegram scan report
            if scored:
                try:
                    await send_scan_report()
                except Exception as e:
                    log.warning("scan_report_failed", error=str(e))

            # Step 3b: Refresh Greeks, prices, and P&L for open positions
            if self._should_refresh_greeks():
                try:
                    refresh_result = refresh_positions()
                    self._last_greeks_refresh = datetime.now(pytz.UTC)
                    log.info("greeks_refreshed", cycle=self._cycle_count, result=refresh_result)
                except Exception as e:
                    log.error("greeks_refresh_failed", error=str(e))

            # Step 4: Portfolio risk assessment
            risk_assessment = calculate_portfolio_risk()

            # Step 5: Get open positions
            positions = get_open_positions()

            # Step 6: Check exit triggers on open positions
            triggered: list[tuple[dict, dict]] = []
            for pos in positions:
                trigger_result = check_exit_triggers(pos)
                if trigger_result.get("should_exit"):
                    triggered.append((pos, trigger_result))

            # Step 7: Filter passing signals + pre-trade checks
            passing: list[tuple[dict, dict, dict]] = []
            for sig, score_result in scored:
                if not score_result.get("passed"):
                    continue
                ptc = pre_trade_check({**sig, "score": score_result.get("score", 0)}, risk_assessment)
                if ptc.get("approved"):
                    passing.append((sig, score_result, ptc))
                else:
                    log.info("pre_trade_denied", ticker=sig.get("ticker"), reasons=ptc.get("reasons"))

            # =========================================================
            # CLAUDE DECISIONS — only when action is needed
            # =========================================================
            claude_calls = 0

            # Fetch market context for Claude decisions
            market_context = None
            if passing or triggered:
                try:
                    from services.alpaca_options_data import get_options_data_client
                    market_context = get_options_data_client().get_market_context()
                    log.info("market_context", **market_context)
                except Exception as e:
                    log.warning("market_context_failed", error=str(e))

            # Entry decisions: Claude evaluates passing signals
            if passing:
                perf_context = self.orchestrator.get_performance_context()
                try:
                    entry_result = await self.orchestrator.evaluate_entries(
                        passing_signals=passing,
                        risk_assessment=risk_assessment,
                        positions=positions,
                        perf_context=perf_context,
                        market_context=market_context,
                    )
                    claude_calls += 1
                    log.info("entry_decision", result_preview=entry_result[:200] if entry_result else "")
                except Exception as e:
                    log.error("entry_evaluation_failed", error=str(e))

            # Exit decisions: Claude evaluates triggered positions
            if triggered:
                try:
                    exit_result = await self.orchestrator.evaluate_exits(
                        triggered_positions=triggered,
                        risk_assessment=risk_assessment,
                        market_context=market_context,
                    )
                    claude_calls += 1
                    log.info("exit_decision", result_preview=exit_result[:200] if exit_result else "")
                except Exception as e:
                    log.error("exit_evaluation_failed", error=str(e))

            # =========================================================
            # CYCLE SUMMARY
            # =========================================================
            passed_count = len(passing)
            rejected_count = len(scored) - passed_count
            log.info(
                "cycle_complete",
                cycle=self._cycle_count,
                signals_scanned=len(signals),
                signals_scored=len(scored),
                signals_passed=passed_count,
                signals_rejected=rejected_count,
                positions_open=len(positions),
                exit_triggers=len(triggered),
                claude_api_calls=claude_calls,
            )
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

    async def _deterministic_position_check(self) -> None:
        """Check positions without Claude (used during trading breaker)."""
        try:
            positions = get_open_positions()
            risk_assessment = calculate_portfolio_risk()
            triggered: list[tuple[dict, dict]] = []
            for pos in positions:
                trigger_result = check_exit_triggers(pos)
                if trigger_result.get("should_exit"):
                    triggered.append((pos, trigger_result))

            if triggered:
                try:
                    await self.orchestrator.evaluate_exits(triggered, risk_assessment, market_context=None)
                except Exception as e:
                    log.error("breaker_exit_eval_failed", error=str(e))

            log.info("position_check_done", positions=len(positions), triggers=len(triggered))
        except Exception as e:
            log.error("position_check_error", error=str(e))

    def _should_refresh_greeks(self) -> bool:
        """Check if enough time has passed to refresh Greeks."""
        if self._last_greeks_refresh is None:
            return True
        elapsed = (datetime.now(pytz.UTC) - self._last_greeks_refresh).total_seconds()
        return elapsed >= self.settings.monitor.greeks_snapshot_interval_seconds

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

        market_close = now.replace(hour=mh.close_hour, minute=mh.close_minute, second=0, microsecond=0)
        minutes_past_close = (now - market_close).total_seconds() / 60

        return 0 < minutes_past_close < 30 and now.weekday() < 5

    async def _send_daily_summary(self) -> None:
        """Send enhanced end-of-day summary via Telegram."""
        self._daily_summary_sent = True
        session = get_session()
        try:
            from analytics.performance import get_performance_summary
            from core.utils import trading_today_et

            today = trading_today_et()

            trades_today = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= today)
                .count()
            )

            trades = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= today)
                .all()
            )
            total_pnl = sum(t.pnl_dollars for t in trades)

            open_count = (
                session.query(PositionRecord)
                .filter(PositionRecord.status == PositionStatus.OPEN)
                .count()
            )

            perf = get_performance_summary(30)

            try:
                account = self._broker.get_account()
                equity = account.get("equity", 0)
            except Exception:
                equity = 0

            from core.circuit_breaker import get_trading_breaker
            breaker = get_trading_breaker()
            breaker_state = breaker.check(equity) if equity else None

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
        """Get adaptive scan interval based on time of day and open positions."""
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

        base_interval = self.settings.monitor.poll_interval_seconds

        # Halve poll interval when open positions exist for more frequent
        # exit trigger, P&L, and risk checks
        session = get_session()
        try:
            has_open = session.query(PositionRecord).filter(
                PositionRecord.status == PositionStatus.OPEN
            ).first() is not None
        finally:
            session.close()

        if has_open:
            return base_interval // 2

        return base_interval

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
