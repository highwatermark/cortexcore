"""
Trading circuit breakers — halt entries on excessive losses.

Four independent breakers:
  1. Daily loss: realized losses today > max_daily_loss_pct of equity
  2. Weekly loss: realized losses this week > max_weekly_loss_pct of equity
  3. Consecutive losses: last N closed trades are all losers
  4. Single trade loss: any open position down > max_single_trade_loss_pct (emergency exit)

State is checked from DB on every call (survives restarts).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from config.settings import get_settings
from core.logger import get_logger
from core.utils import ensure_utc
from data.models import TradeLog, get_session

log = get_logger("circuit_breaker")


class BreakerState(NamedTuple):
    is_tripped: bool
    reason: str
    resumes_at: str  # ISO datetime or ""


class TradingCircuitBreaker:
    """Check whether trading should be halted due to loss limits."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def check(self, equity: float) -> BreakerState:
        """Run all breaker checks. Returns first tripped breaker or clear state."""
        checks = [
            self._check_daily_loss(equity),
            self._check_weekly_loss(equity),
            self._check_consecutive_losses(),
        ]
        for state in checks:
            if state.is_tripped:
                log.warning(
                    "trading_breaker_tripped",
                    reason=state.reason,
                    resumes_at=state.resumes_at,
                )
                return state

        return BreakerState(is_tripped=False, reason="", resumes_at="")

    def _check_daily_loss(self, equity: float) -> BreakerState:
        max_pct = self._settings.monitor.max_daily_loss_pct
        session = get_session()
        try:
            from core.utils import trading_today, trading_now
            today = trading_today()
            now = trading_now()
            trades = (
                session.query(TradeLog)
                .filter(
                    TradeLog.closed_at >= today,
                    TradeLog.exit_reason != "phantom_closure_reconciler",
                )
                .all()
            )
            total_loss = sum(t.pnl_dollars for t in trades if t.pnl_dollars < 0)
            loss_pct = abs(total_loss) / equity if equity > 0 else 0

            if loss_pct >= max_pct:
                # Resumes next trading day (approximate: tomorrow 9:30 ET)
                tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d 06:30 PT")
                return BreakerState(
                    is_tripped=True,
                    reason=f"Daily loss {loss_pct:.1%} >= {max_pct:.0%} (${abs(total_loss):.0f})",
                    resumes_at=tomorrow,
                )
            return BreakerState(is_tripped=False, reason="", resumes_at="")
        finally:
            session.close()

    def _check_weekly_loss(self, equity: float) -> BreakerState:
        max_pct = self._settings.monitor.max_weekly_loss_pct
        session = get_session()
        try:
            from core.utils import trading_now
            now = trading_now()
            monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            trades = (
                session.query(TradeLog)
                .filter(
                    TradeLog.closed_at >= monday,
                    TradeLog.exit_reason != "phantom_closure_reconciler",
                )
                .all()
            )
            total_loss = sum(t.pnl_dollars for t in trades if t.pnl_dollars < 0)
            loss_pct = abs(total_loss) / equity if equity > 0 else 0

            if loss_pct >= max_pct:
                # Resumes next Monday
                days_to_monday = 7 - now.weekday()
                next_monday = (now + timedelta(days=days_to_monday)).strftime("%Y-%m-%d 06:30 PT")
                return BreakerState(
                    is_tripped=True,
                    reason=f"Weekly loss {loss_pct:.1%} >= {max_pct:.0%} (${abs(total_loss):.0f})",
                    resumes_at=next_monday,
                )
            return BreakerState(is_tripped=False, reason="", resumes_at="")
        finally:
            session.close()

    def _check_consecutive_losses(self) -> BreakerState:
        max_consecutive = self._settings.monitor.max_consecutive_losses
        cooldown = self._settings.monitor.loss_cooldown_minutes
        session = get_session()
        try:
            recent = (
                session.query(TradeLog)
                .filter(TradeLog.exit_reason != "phantom_closure_reconciler")
                .order_by(TradeLog.closed_at.desc())
                .limit(max_consecutive)
                .all()
            )
            if len(recent) < max_consecutive:
                return BreakerState(is_tripped=False, reason="", resumes_at="")

            all_losses = all(t.pnl_dollars < 0 for t in recent)
            if not all_losses:
                return BreakerState(is_tripped=False, reason="", resumes_at="")

            # Check if cooldown has passed since last loss
            last_loss_time = ensure_utc(recent[0].closed_at)

            if last_loss_time:
                resumes = last_loss_time + timedelta(minutes=cooldown)
                now = datetime.now(timezone.utc)
                if now >= resumes:
                    return BreakerState(is_tripped=False, reason="", resumes_at="")

                return BreakerState(
                    is_tripped=True,
                    reason=f"Last {max_consecutive} trades all losses — cooling off {cooldown} min",
                    resumes_at=resumes.isoformat(),
                )

            return BreakerState(
                is_tripped=True,
                reason=f"Last {max_consecutive} trades all losses",
                resumes_at="",
            )
        finally:
            session.close()


    def check_emergency_exits(self) -> list[dict]:
        """Check if any open position exceeds the single-trade loss limit.

        Returns list of position dicts that need immediate emergency exit.
        Does NOT trip the trading breaker — just identifies positions to close.
        """
        max_loss = self._settings.monitor.max_single_trade_loss_pct
        from services.alpaca_broker import get_broker

        try:
            broker_positions = get_broker().get_positions()
        except Exception:
            log.warning("emergency_exit_check_broker_unreachable")
            return []

        emergency_exits = []
        for bp in broker_positions:
            if bp.entry_price and bp.entry_price > 0:
                loss_pct = (bp.entry_price - bp.current_price) / bp.entry_price
                if loss_pct >= max_loss:
                    emergency_exits.append({
                        "option_symbol": bp.option_symbol,
                        "ticker": bp.ticker,
                        "loss_pct": round(loss_pct * 100, 1),
                        "entry_price": bp.entry_price,
                        "current_price": bp.current_price,
                    })
                    log.warning(
                        "emergency_exit_triggered",
                        ticker=bp.ticker,
                        loss_pct=f"{loss_pct:.1%}",
                        max_loss=f"{max_loss:.0%}",
                    )
        return emergency_exits


_breaker: TradingCircuitBreaker | None = None


def get_trading_breaker() -> TradingCircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = TradingCircuitBreaker()
    return _breaker
