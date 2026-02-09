"""
Interactive Telegram bot for monitoring and controlling the trading agent.

Long-polling bot that runs as an asyncio task within the monitor loop.
Processes commands only from the authorized admin chat.

Commands:
  /health       — Run health checks, return status
  /status       — Mode, uptime, scan count, circuit breaker state
  /positions    — Open positions with P&L, DTE
  /orders       — Pending broker orders
  /expirations  — DTE alerts for open positions
  /risk         — Portfolio risk score breakdown
  /performance  — 30-day metrics (win rate, Sharpe, drawdown)
  /weekly       — 7-day performance report
  /history      — Last 10 trades with P&L
  /flow         — Trigger manual flow scan
  /close ID     — Close a position by ID or ticker
  /reconcile    — Sync positions with broker
  /killswitch   — Toggle kill switch on/off
  /errors       — Last 10 error log entries
  /help         — List available commands
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from config.settings import get_settings
from core.logger import get_logger

log = get_logger("telegram_bot")

TELEGRAM_API = "https://api.telegram.org"
POLL_TIMEOUT = 30  # seconds for long-polling
RATE_LIMIT_SECONDS = 5


class TelegramBot:
    """Long-polling Telegram bot for operator commands."""

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.api.telegram_bot_token
        self._admin_chat_id = settings.api.telegram_chat_id
        self._enabled = bool(self._token and self._admin_chat_id)
        self._offset = 0  # Telegram update offset
        self._last_command_time = 0.0
        self._start_time = time.time()
        self._running = False

        if not self._enabled:
            log.warning("telegram_bot_disabled", reason="missing token or chat_id")

    async def start(self) -> None:
        """Start the bot polling loop. Runs until stopped."""
        if not self._enabled:
            return

        self._running = True
        log.info("telegram_bot_started")

        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("telegram_bot_error", error=str(e))
                await asyncio.sleep(5)

        log.info("telegram_bot_stopped")

    def stop(self) -> None:
        """Signal the bot to stop."""
        self._running = False

    async def _get_updates(self) -> list[dict]:
        """Long-poll for new messages from Telegram."""
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 5) as client:
                resp = await client.get(
                    f"{TELEGRAM_API}/bot{self._token}/getUpdates",
                    params={
                        "offset": self._offset,
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": '["message"]',
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if not data.get("ok"):
                    return []

                updates = data.get("result", [])
                if updates:
                    self._offset = updates[-1]["update_id"] + 1
                return updates
        except httpx.TimeoutException:
            return []  # Normal for long-polling
        except Exception as e:
            log.error("telegram_poll_error", error=str(e))
            await asyncio.sleep(2)
            return []

    async def _handle_update(self, update: dict) -> None:
        """Process a single Telegram update."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        # Auth check — only process from admin
        if chat_id != self._admin_chat_id:
            log.warning("telegram_unauthorized", chat_id=chat_id)
            return

        if not text.startswith("/"):
            return

        # Rate limiting
        now = time.time()
        if now - self._last_command_time < RATE_LIMIT_SECONDS:
            await self._reply(chat_id, "Rate limited. Wait a few seconds.")
            return
        self._last_command_time = now

        # Parse command
        parts = text.split()
        command = parts[0].lower().split("@")[0]  # Handle /command@botname
        args = parts[1:] if len(parts) > 1 else []

        log.info("telegram_command", command=command, args=args)

        handlers = {
            "/health": self._cmd_health,
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/orders": self._cmd_orders,
            "/expirations": self._cmd_expirations,
            "/exp": self._cmd_expirations,
            "/risk": self._cmd_risk,
            "/performance": self._cmd_performance,
            "/perf": self._cmd_performance,
            "/weekly": self._cmd_weekly,
            "/history": self._cmd_history,
            "/flow": self._cmd_flow,
            "/close": self._cmd_close,
            "/reconcile": self._cmd_reconcile,
            "/killswitch": self._cmd_killswitch,
            "/kill": self._cmd_killswitch,
            "/errors": self._cmd_errors,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }

        handler = handlers.get(command)
        if handler:
            try:
                response = await handler(args)
                await self._reply(chat_id, response)
            except Exception as e:
                log.error("command_error", command=command, error=str(e))
                await self._reply(chat_id, f"Error: {str(e)[:200]}")
        else:
            await self._reply(chat_id, f"Unknown command: {command}\nType /help for available commands.")

    async def _reply(self, chat_id: str, text: str) -> None:
        """Send a reply message."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
        except Exception as e:
            log.error("telegram_reply_error", error=str(e))

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------

    async def _cmd_health(self, args: list[str]) -> str:
        """Run health checks and return status."""
        from core.health import get_health_checker

        checker = get_health_checker()
        results = await checker.run_all()

        lines = ["<b>Health Check</b>"]
        for r in results:
            icon = "OK" if r.ok else "FAIL"
            lines.append(f"  [{icon}] {r.name}: {r.detail} ({r.latency_ms:.0f}ms)")

        overall = "HEALTHY" if all(r.ok for r in results) else "UNHEALTHY"
        lines.append(f"\nOverall: <b>{overall}</b>")
        return "\n".join(lines)

    async def _cmd_status(self, args: list[str]) -> str:
        """Show system status."""
        from core.killswitch import is_killed
        from core.circuit_breaker import get_trading_breaker
        from services.alpaca_broker import get_broker

        settings = get_settings()
        uptime_sec = time.time() - self._start_time
        hours = int(uptime_sec // 3600)
        minutes = int((uptime_sec % 3600) // 60)

        killed = is_killed()

        try:
            broker = get_broker()
            account = broker.get_account()
            equity = account.get("equity", 0)
            breaker = get_trading_breaker()
            breaker_state = breaker.check(equity)
        except Exception:
            equity = 0
            breaker_state = None

        lines = [
            "<b>System Status</b>",
            f"  Mode: {'PAPER' if settings.paper_trading else 'LIVE'}",
            f"  Shadow: {'ON' if settings.shadow_mode else 'OFF'}",
            f"  Uptime: {hours}h {minutes}m",
            f"  Kill switch: {'ENGAGED' if killed else 'OFF'}",
            f"  Equity: ${equity:,.0f}",
        ]

        if breaker_state and breaker_state.is_tripped:
            lines.append(f"  Circuit breaker: TRIPPED — {breaker_state.reason}")
        else:
            lines.append("  Circuit breaker: Clear")

        return "\n".join(lines)

    async def _cmd_positions(self, args: list[str]) -> str:
        """Show open positions."""
        from tools.position_tools import get_open_positions

        try:
            positions = get_open_positions()
        except Exception as e:
            return f"Error fetching positions: {str(e)[:100]}"

        if not positions:
            return "No open positions."

        lines = [f"<b>Open Positions ({len(positions)})</b>"]
        for p in positions:
            pnl_sign = "+" if p["pnl_dollars"] >= 0 else ""
            lines.append(
                f"\n  <b>{p['ticker']}</b> {p['action']} ${p['strike']} "
                f"exp {p['expiration']}\n"
                f"  Qty: {p['quantity']} | Entry: ${p['entry_price']:.2f} | "
                f"Now: ${p['current_price']:.2f}\n"
                f"  P&L: {pnl_sign}${p['pnl_dollars']:.2f} ({pnl_sign}{p['pnl_pct']:.1f}%) | "
                f"DTE: {p['dte_remaining']}"
            )
        return "\n".join(lines)

    async def _cmd_risk(self, args: list[str]) -> str:
        """Show portfolio risk assessment."""
        from tools.risk_tools import calculate_portfolio_risk

        try:
            risk = calculate_portfolio_risk()
        except Exception as e:
            return f"Error calculating risk: {str(e)[:100]}"

        lines = [
            "<b>Portfolio Risk</b>",
            f"  Risk score: {risk.get('risk_score', 0)}/100",
            f"  Level: {risk.get('risk_level', 'UNKNOWN')}",
            f"  Position count: {risk.get('position_count', 0)}",
            f"  Total exposure: ${risk.get('total_exposure', 0):,.0f}",
        ]

        warnings = risk.get("warnings", [])
        if warnings:
            lines.append("\nWarnings:")
            for w in warnings[:5]:
                lines.append(f"  - {w}")

        return "\n".join(lines)

    async def _cmd_performance(self, args: list[str]) -> str:
        """Show 30-day performance metrics."""
        from analytics.performance import get_performance_summary

        perf = get_performance_summary(30)

        pf = perf["profit_factor"]
        pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)

        lines = [
            "<b>30-Day Performance</b>",
            f"  Total trades: {perf['total_trades']}",
            f"  Total P&L: ${perf['total_pnl']:+,.2f}",
            f"  Win rate: {perf['win_rate']:.0%}",
            f"  Profit factor: {pf_str}",
            f"  Max drawdown: {perf['max_drawdown']:.1%}",
            f"  Sharpe ratio: {perf['sharpe_ratio']}",
            f"  Avg hold: {perf['avg_hold_hours']}h",
        ]

        daily = perf.get("daily_pnl", [])
        if daily:
            lines.append("\nRecent daily P&L:")
            for d in daily[-5:]:
                sign = "+" if d["pnl"] >= 0 else ""
                lines.append(f"  {d['date']}: {sign}${d['pnl']:.2f}")

        return "\n".join(lines)

    async def _cmd_killswitch(self, args: list[str]) -> str:
        """Toggle kill switch."""
        from core.killswitch import is_killed, engage, disengage

        if not args:
            status = "ENGAGED" if is_killed() else "OFF"
            return f"Kill switch is <b>{status}</b>\n\nUsage: /killswitch on|off"

        action = args[0].lower()
        if action in ("on", "engage"):
            engage("Engaged via Telegram /killswitch command")
            return "Kill switch <b>ENGAGED</b> — all trading halted."
        elif action in ("off", "disengage"):
            disengage()
            return "Kill switch <b>DISENGAGED</b> — trading will resume."
        else:
            return "Usage: /killswitch on|off"

    async def _cmd_errors(self, args: list[str]) -> str:
        """Show recent errors from log file."""
        from pathlib import Path

        log_dir = Path(get_settings().log_dir)
        log_files = sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)

        if not log_files:
            return "No log files found."

        errors = []
        for log_file in log_files[:2]:
            try:
                with open(log_file) as f:
                    for line in f:
                        if "[error" in line.lower() or "[critical" in line.lower():
                            errors.append(line.strip()[:150])
            except Exception:
                pass

        if not errors:
            return "No recent errors found."

        # Last 10 errors
        recent = errors[-10:]
        lines = [f"<b>Recent Errors ({len(recent)})</b>"]
        for e in recent:
            lines.append(f"  {e}")
        return "\n".join(lines)

    async def _cmd_orders(self, args: list[str]) -> str:
        """Show open/pending broker orders."""
        from data.models import BrokerOrder, OrderStatus, get_session

        session = get_session()
        try:
            pending = (
                session.query(BrokerOrder)
                .filter(BrokerOrder.status.in_([OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL]))
                .all()
            )
            if not pending:
                return "No open orders."

            lines = [f"<b>Open Orders ({len(pending)})</b>"]
            for o in pending:
                fill_info = f" ({o.filled_qty} filled)" if o.filled_qty else ""
                lines.append(
                    f"\n  {o.ticker} {o.side.value} x{o.quantity}{fill_info}\n"
                    f"  {o.order_type} @ ${o.limit_price:.2f} | {o.status.value}\n"
                    f"  ID: {o.broker_order_id[:12]}..."
                )
            return "\n".join(lines)
        finally:
            session.close()

    async def _cmd_history(self, args: list[str]) -> str:
        """Show recent trade history with P&L."""
        from data.models import TradeLog, get_session

        session = get_session()
        try:
            trades = (
                session.query(TradeLog)
                .order_by(TradeLog.closed_at.desc())
                .limit(10)
                .all()
            )
            if not trades:
                return "No trade history yet."

            wins = sum(1 for t in trades if t.pnl_dollars > 0)
            losses = len(trades) - wins
            win_rate = wins / len(trades) * 100 if trades else 0

            lines = [
                f"<b>Trade History</b>",
                f"  Record: {wins}W / {losses}L ({win_rate:.0f}% win rate)\n",
            ]
            for t in trades:
                icon = "W" if t.pnl_dollars > 0 else "L"
                pnl_sign = "+" if t.pnl_dollars >= 0 else ""
                lines.append(
                    f"  [{icon}] {t.ticker}: {pnl_sign}{t.pnl_pct:.1f}% "
                    f"(${pnl_sign}{t.pnl_dollars:.0f}) — {t.exit_reason or 'N/A'}"
                )
            return "\n".join(lines)
        finally:
            session.close()

    async def _cmd_expirations(self, args: list[str]) -> str:
        """Show DTE alerts for open positions."""
        from data.models import PositionRecord, PositionStatus, get_session
        from core.utils import calc_dte

        session = get_session()
        try:
            positions = (
                session.query(PositionRecord)
                .filter(PositionRecord.status == PositionStatus.OPEN)
                .all()
            )
            if not positions:
                return "No open positions."

            alerts = []
            for p in positions:
                dte = calc_dte(p.expiration) if p.expiration else 999
                if dte <= 14:
                    if dte <= 3:
                        severity, label = "CRITICAL", "[!!!]"
                    elif dte <= 5:
                        severity, label = "HIGH", "[!!]"
                    elif dte <= 7:
                        severity, label = "MEDIUM", "[!]"
                    else:
                        severity, label = "LOW", "[i]"
                    alerts.append((dte, severity, label, p))

            if not alerts:
                return "No expiration concerns. All positions have adequate DTE."

            alerts.sort(key=lambda x: x[0])
            lines = [f"<b>Expiration Alerts ({len(alerts)})</b>"]
            for dte, severity, label, p in alerts:
                lines.append(
                    f"\n  {label} <b>{p.ticker}</b> {p.action.value if p.action else '?'} "
                    f"${p.strike} exp {p.expiration}\n"
                    f"  DTE: {dte} | Severity: {severity}"
                )
                if dte <= 5:
                    lines.append("  Action: Close or roll immediately")

            lines.append("\nUse /close POSITION_ID to exit.")
            return "\n".join(lines)
        finally:
            session.close()

    async def _cmd_weekly(self, args: list[str]) -> str:
        """Show last 7 days performance report."""
        from data.models import TradeLog, get_session
        from analytics.performance import get_performance_summary

        session = get_session()
        try:
            perf = get_performance_summary(7)

            # Get individual trades for best/worst
            week_start = (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=7)).strftime("%Y-%m-%d")
            trades = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= week_start)
                .order_by(TradeLog.pnl_pct.desc())
                .all()
            )

            pf = perf["profit_factor"]
            pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)

            lines = [
                "<b>Weekly Report (7 days)</b>\n",
                "<b>Performance:</b>",
                f"  P&amp;L: ${perf['total_pnl']:+,.2f}",
                f"  Trades: {perf['total_trades']}",
                f"  Win rate: {perf['win_rate']:.0%}",
                f"  Profit factor: {pf_str}",
                f"  Max drawdown: {perf['max_drawdown']:.1%}",
            ]

            if trades:
                best = trades[0]
                worst = trades[-1]
                lines.append(f"\n  Best: {best.ticker} +{best.pnl_pct:.1f}% (${best.pnl_dollars:+,.0f})")
                if worst.pnl_dollars < 0:
                    lines.append(f"  Worst: {worst.ticker} {worst.pnl_pct:.1f}% (${worst.pnl_dollars:+,.0f})")

            daily = perf.get("daily_pnl", [])
            if daily:
                lines.append("\n<b>Daily P&amp;L:</b>")
                for d in daily:
                    sign = "+" if d["pnl"] >= 0 else ""
                    lines.append(f"  {d['date']}: {sign}${d['pnl']:.2f}")

            return "\n".join(lines)
        finally:
            session.close()

    async def _cmd_flow(self, args: list[str]) -> str:
        """Trigger a manual flow scan via the orchestrator."""
        from agents.orchestrator import Orchestrator

        try:
            orchestrator = Orchestrator()
            result = await orchestrator.run_scan_cycle()
            preview = result[:500] if result else "No result"
            return f"<b>Flow Scan Complete</b>\n\n{preview}"
        except Exception as e:
            return f"Flow scan failed: {str(e)[:200]}"

    async def _cmd_reconcile(self, args: list[str]) -> str:
        """Trigger position reconciliation."""
        from core.reconciler import reconcile_positions

        try:
            result = await reconcile_positions()
            orphans = result.get("orphans_adopted", 0)
            phantoms = result.get("phantoms_closed", 0)
            drifts = result.get("prices_corrected", 0)

            if orphans == 0 and phantoms == 0 and drifts == 0:
                return (
                    "<b>Reconciliation Complete</b>\n"
                    "  All positions synced. No issues found."
                )

            lines = [
                "<b>Reconciliation Complete</b>\n",
                f"  Orphans adopted: {orphans}",
                f"  Phantoms closed: {phantoms}",
                f"  Prices corrected: {drifts}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Reconciliation failed: {str(e)[:200]}"

    async def _cmd_close(self, args: list[str]) -> str:
        """Close an open position by position_id or ticker.

        Usage: /close POSITION_ID [reason]
               /close TICKER [reason]
        """
        if not args:
            return "Usage: /close POSITION_ID [reason]\n       /close TICKER [reason]"

        from data.models import PositionRecord, PositionStatus, get_session
        from tools.execution_tools import execute_exit

        target = args[0].upper()
        reason = " ".join(args[1:]) if len(args) > 1 else "manual via Telegram"

        session = get_session()
        try:
            # Try by position_id first, then by ticker
            pos = session.query(PositionRecord).filter(
                PositionRecord.position_id == args[0],
                PositionRecord.status == PositionStatus.OPEN,
            ).first()

            if not pos:
                pos = session.query(PositionRecord).filter(
                    PositionRecord.ticker == target,
                    PositionRecord.status == PositionStatus.OPEN,
                ).first()

            if not pos:
                return f"No open position found for '{target}'"

            position_id = pos.position_id
            ticker = pos.ticker
        finally:
            session.close()

        try:
            result = await execute_exit(position_id=position_id, reason=reason)

            if result.get("success"):
                pnl = result.get("pnl_dollars", 0)
                pnl_pct = result.get("pnl_pct", 0)
                sign = "+" if pnl >= 0 else ""
                return (
                    f"<b>Position Closed: {ticker}</b>\n"
                    f"  P&amp;L: {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)\n"
                    f"  {result.get('message', '')}"
                )
            else:
                return f"Close failed: {result.get('error', 'unknown')}"
        except Exception as e:
            return f"Close error: {str(e)[:200]}"

    async def _cmd_help(self, args: list[str]) -> str:
        """List available commands."""
        return (
            "<b>Momentum Agent Commands</b>\n\n"
            "<b>Monitoring:</b>\n"
            "/health — Run health checks\n"
            "/status — System status and mode\n"
            "/positions — Open positions with P&L\n"
            "/orders — Pending broker orders\n"
            "/expirations — DTE alerts for positions\n"
            "\n<b>Trading:</b>\n"
            "/flow — Trigger manual flow scan\n"
            "/close ID|TICKER — Close a position\n"
            "/reconcile — Sync positions with broker\n"
            "\n<b>Analytics:</b>\n"
            "/performance — 30-day metrics\n"
            "/weekly — 7-day report\n"
            "/history — Last 10 trades\n"
            "\n<b>System:</b>\n"
            "/risk — Portfolio risk assessment\n"
            "/killswitch on|off — Toggle kill switch\n"
            "/errors — Recent error log entries\n"
            "/help — This message"
        )
