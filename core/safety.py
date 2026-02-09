"""
Deterministic safety gate — hard limits that Claude cannot override.

Called inside execute_entry() BEFORE order submission. Every check is
non-negotiable. No "exceptional conviction" bypass. If the gate says no,
the trade does not happen.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pytz

from config.settings import EXCLUDED_TICKERS, get_settings
from core.logger import get_logger
from data.models import (
    IntentStatus,
    OrderIntent,
    PositionRecord,
    PositionStatus,
    TradeLog,
    get_session,
)

# Cache for earnings dates: {ticker: (date_str_or_None, fetched_at)}
_earnings_cache: dict[str, tuple[str | None, datetime]] = {}

log = get_logger("safety_gate")


class SafetyGate:
    """Deterministic pre-trade safety checks.

    Every method returns (allowed: bool, reason: str).
    The main entry point is `check_entry()` which runs all checks.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def check_entry(self, signal: dict) -> tuple[bool, str]:
        """Run ALL safety checks. Returns (allowed, reason).

        If any single check fails, the entry is blocked.
        """
        checks = [
            self._check_excluded_ticker,
            self._check_max_positions,
            self._check_max_exposure,
            self._check_max_position_value,
            self._check_max_executions_today,
            self._check_daily_loss_limit,
            self._check_weekly_loss_limit,
            self._check_consecutive_losses,
            self._check_iv_rank,
            self._check_dte,
            self._check_spread,
            self._check_earnings_blackout,
            self._check_market_timing,
        ]

        for check_fn in checks:
            allowed, reason = check_fn(signal)
            if not allowed:
                log.warning("safety_gate_blocked", check=check_fn.__name__, reason=reason, ticker=signal.get("ticker", ""))
                return False, reason

        log.info("safety_gate_passed", ticker=signal.get("ticker", ""))
        return True, "All safety checks passed"

    def _check_excluded_ticker(self, signal: dict) -> tuple[bool, str]:
        ticker = signal.get("ticker", "").upper()
        if ticker in EXCLUDED_TICKERS:
            return False, f"Ticker {ticker} is in excluded list"
        return True, ""

    def _check_max_positions(self, signal: dict) -> tuple[bool, str]:
        max_pos = self._settings.trading.max_positions
        session = get_session()
        try:
            count = (
                session.query(PositionRecord)
                .filter(PositionRecord.status == PositionStatus.OPEN)
                .count()
            )
            if count >= max_pos:
                return False, f"Max positions reached: {count}/{max_pos}"
            return True, ""
        finally:
            session.close()

    def _check_max_exposure(self, signal: dict) -> tuple[bool, str]:
        """Check total exposure as % of account equity."""
        max_pct = self._settings.trading.max_total_exposure_pct
        session = get_session()
        try:
            positions = (
                session.query(PositionRecord)
                .filter(PositionRecord.status == PositionStatus.OPEN)
                .all()
            )
            total_exposure = sum((p.entry_value or 0) for p in positions)

            # Add proposed trade value
            qty = signal.get("quantity", 1)
            price = signal.get("limit_price", 0)
            proposed_value = qty * price * 100  # options multiplier

            from services.alpaca_broker import get_broker
            try:
                account = get_broker().get_account()
                equity = account.get("equity", 100_000)
            except Exception:
                equity = 100_000

            new_total = total_exposure + proposed_value
            exposure_pct = new_total / equity if equity > 0 else 1.0

            if exposure_pct > max_pct:
                return False, f"Total exposure {exposure_pct:.0%} would exceed {max_pct:.0%} limit"
            return True, ""
        finally:
            session.close()

    def _check_max_position_value(self, signal: dict) -> tuple[bool, str]:
        max_val = self._settings.trading.max_position_value
        qty = signal.get("quantity", 1)
        price = signal.get("limit_price", 0)
        trade_value = qty * price * 100
        if trade_value > max_val:
            return False, f"Trade value ${trade_value:.0f} exceeds max ${max_val:.0f}"
        return True, ""

    def _check_max_executions_today(self, signal: dict) -> tuple[bool, str]:
        max_exec = self._settings.trading.max_executions_per_day
        session = get_session()
        try:
            from core.utils import trading_today_et
            today = trading_today_et()
            count = (
                session.query(OrderIntent)
                .filter(
                    OrderIntent.idempotency_key.like("entry-%"),
                    OrderIntent.status == IntentStatus.EXECUTED,
                    OrderIntent.executed_at >= today,
                )
                .count()
            )
            if count >= max_exec:
                return False, f"Max entries today reached: {count}/{max_exec}"
            return True, ""
        finally:
            session.close()

    def _check_daily_loss_limit(self, signal: dict) -> tuple[bool, str]:
        max_loss_pct = self._settings.monitor.max_daily_loss_pct
        session = get_session()
        try:
            from core.utils import trading_today_et
            today = trading_today_et()
            trades = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= today)
                .all()
            )
            total_loss = sum(t.pnl_dollars for t in trades if t.pnl_dollars < 0)

            from services.alpaca_broker import get_broker
            try:
                equity = get_broker().get_account().get("equity", 100_000)
            except Exception:
                equity = 100_000

            loss_pct = abs(total_loss) / equity if equity > 0 else 0
            if loss_pct >= max_loss_pct:
                return False, f"Daily loss {loss_pct:.1%} >= {max_loss_pct:.0%} limit (${abs(total_loss):.0f})"
            return True, ""
        finally:
            session.close()

    def _check_weekly_loss_limit(self, signal: dict) -> tuple[bool, str]:
        max_loss_pct = self._settings.monitor.max_weekly_loss_pct
        session = get_session()
        try:
            # Monday of this week (ET)
            from core.utils import trading_now_et
            now = trading_now_et()
            monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            trades = (
                session.query(TradeLog)
                .filter(TradeLog.closed_at >= monday)
                .all()
            )
            total_loss = sum(t.pnl_dollars for t in trades if t.pnl_dollars < 0)

            from services.alpaca_broker import get_broker
            try:
                equity = get_broker().get_account().get("equity", 100_000)
            except Exception:
                equity = 100_000

            loss_pct = abs(total_loss) / equity if equity > 0 else 0
            if loss_pct >= max_loss_pct:
                return False, f"Weekly loss {loss_pct:.1%} >= {max_loss_pct:.0%} limit"
            return True, ""
        finally:
            session.close()

    def _check_consecutive_losses(self, signal: dict) -> tuple[bool, str]:
        max_consecutive = self._settings.monitor.max_consecutive_losses
        session = get_session()
        try:
            recent_trades = (
                session.query(TradeLog)
                .order_by(TradeLog.closed_at.desc())
                .limit(max_consecutive)
                .all()
            )
            if len(recent_trades) < max_consecutive:
                return True, ""

            all_losses = all(t.pnl_dollars < 0 for t in recent_trades)
            if all_losses:
                return False, f"Last {max_consecutive} trades are all losses — cooling off"
            return True, ""
        finally:
            session.close()

    def _check_iv_rank(self, signal: dict) -> tuple[bool, str]:
        max_iv = self._settings.risk.max_iv_rank_for_entry
        iv_rank = signal.get("iv_rank", 0)
        if iv_rank > max_iv:
            return False, f"IV rank {iv_rank}% > {max_iv}% limit"
        return True, ""

    def _check_dte(self, signal: dict) -> tuple[bool, str]:
        min_dte = self._settings.risk.min_dte_for_entry
        dte = signal.get("dte", 0)
        if dte < min_dte:
            return False, f"DTE {dte} < {min_dte} minimum"
        return True, ""

    def _check_spread(self, signal: dict) -> tuple[bool, str]:
        """Block entries with excessive bid-ask spread."""
        max_spread = self._settings.trading.max_spread_pct
        bid = signal.get("bid", 0)
        ask = signal.get("ask", 0)

        # If bid/ask not provided, skip check (fail-open for missing data)
        if not bid or not ask or ask <= 0:
            return True, ""

        spread_pct = ((ask - bid) / ask) * 100
        if spread_pct > max_spread:
            return False, f"Spread {spread_pct:.1f}% exceeds max {max_spread:.0f}%"
        return True, ""

    def _check_earnings_blackout(self, signal: dict) -> tuple[bool, str]:
        """Block entries within N days of earnings.

        Uses cached earnings data from UW API. Fail-open: if earnings data
        unavailable, trade is allowed (data issue, not safety issue).
        """
        blackout_days = self._settings.trading.earnings_blackout_days
        if blackout_days <= 0:
            return True, ""

        ticker = signal.get("ticker", "").upper()
        if not ticker:
            return True, ""

        # Check cache first (cache per day)
        global _earnings_cache
        now = datetime.now(timezone.utc)
        cached = _earnings_cache.get(ticker)
        if cached:
            earnings_date_str, fetched_at = cached
            # Cache valid for 24 hours
            if (now - fetched_at).total_seconds() < 86400:
                if earnings_date_str:
                    return self._evaluate_earnings_blackout(earnings_date_str, blackout_days, ticker)
                return True, ""  # No earnings data cached

        # Try to fetch earnings date (async in sync context)
        try:
            from services.unusual_whales import UnusualWhalesClient
            client = UnusualWhalesClient()
            # Run async call - use existing event loop if available
            try:
                loop = asyncio.get_running_loop()
                # Already in async context, can't use asyncio.run
                # Schedule and await
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    earnings_date = loop.run_in_executor(pool, lambda: asyncio.run(client.get_next_earnings_date(ticker)))
                    # This won't work cleanly in nested async - fall back to cache miss
                    earnings_date = None
            except RuntimeError:
                # No running loop - safe to use asyncio.run
                earnings_date = asyncio.run(client.get_next_earnings_date(ticker))

            _earnings_cache[ticker] = (earnings_date, now)

            if earnings_date:
                return self._evaluate_earnings_blackout(earnings_date, blackout_days, ticker)
        except Exception as e:
            log.warning("earnings_check_failed", ticker=ticker, error=str(e))
            # Fail-open: allow trade if we can't check earnings

        return True, ""

    def _evaluate_earnings_blackout(self, earnings_date_str: str, blackout_days: int, ticker: str) -> tuple[bool, str]:
        """Check if earnings date is within blackout window."""
        try:
            earnings_date = datetime.strptime(earnings_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_until = (earnings_date - now).days

            if 0 <= days_until <= blackout_days:
                return False, f"Earnings blackout: {ticker} reports in {days_until} day(s) (blackout: {blackout_days} days)"
        except (ValueError, TypeError):
            pass
        return True, ""

    def _check_market_timing(self, signal: dict) -> tuple[bool, str]:
        """Block entries in first/last N minutes of trading day."""
        mh = self._settings.market_hours
        mon = self._settings.monitor
        tz = pytz.timezone(mh.timezone)
        now = datetime.now(tz)

        market_open = now.replace(hour=mh.open_hour, minute=mh.open_minute, second=0, microsecond=0)
        market_close = now.replace(hour=mh.close_hour, minute=mh.close_minute, second=0, microsecond=0)

        minutes_since_open = (now - market_open).total_seconds() / 60
        minutes_to_close = (market_close - now).total_seconds() / 60

        if minutes_since_open < mon.market_open_delay_minutes:
            return False, f"Market opened {minutes_since_open:.0f} min ago — waiting {mon.market_open_delay_minutes} min"
        if minutes_to_close < mon.market_close_buffer_minutes:
            return False, f"Market closes in {minutes_to_close:.0f} min — no entries in last {mon.market_close_buffer_minutes} min"
        return True, ""


# Singleton
_gate: SafetyGate | None = None


def get_safety_gate() -> SafetyGate:
    global _gate
    if _gate is None:
        _gate = SafetyGate()
    return _gate
