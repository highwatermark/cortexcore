"""
Performance analytics — pure SQL aggregate queries on TradeLog.

All functions accept a `days` parameter for the lookback window.
No ML, no complexity — straightforward calculations for monitoring and reporting.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from core.logger import get_logger
from data.models import TradeLog, get_session

log = get_logger("analytics")

RISK_FREE_RATE = 0.05  # 5% annualized


def _get_trades(days: int = 30) -> list[TradeLog]:
    """Fetch closed trades within the lookback window."""
    session = get_session()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        return (
            session.query(TradeLog)
            .filter(TradeLog.closed_at >= cutoff)
            .order_by(TradeLog.closed_at.asc())
            .all()
        )
    finally:
        session.close()


def get_win_rate(days: int = 30) -> float:
    """Percentage of trades closed at profit (0.0 - 1.0)."""
    trades = _get_trades(days)
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.pnl_dollars > 0)
    return wins / len(trades)


def get_profit_factor(days: int = 30) -> float:
    """Sum of wins / sum of losses. >1.0 is profitable."""
    trades = _get_trades(days)
    gross_profit = sum(t.pnl_dollars for t in trades if t.pnl_dollars > 0)
    gross_loss = abs(sum(t.pnl_dollars for t in trades if t.pnl_dollars < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def get_max_drawdown(days: int = 30) -> float:
    """Peak-to-trough equity decline as a fraction (0.0 - 1.0).

    Computed from cumulative P&L series.
    """
    trades = _get_trades(days)
    if not trades:
        return 0.0

    # Build cumulative equity curve (starting at 0)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for t in trades:
        cumulative += t.pnl_dollars
        if cumulative > peak:
            peak = cumulative
        if peak > 0:
            dd = (peak - cumulative) / peak
            if dd > max_dd:
                max_dd = dd

    return max_dd


def get_sharpe_ratio(days: int = 30) -> float:
    """Annualized Sharpe ratio (risk-free = 5%).

    Uses daily P&L returns grouped by day.
    """
    trades = _get_trades(days)
    if len(trades) < 2:
        return 0.0

    # Group P&L by day
    daily_pnl: dict[str, float] = {}
    for t in trades:
        day = t.closed_at.strftime("%Y-%m-%d") if t.closed_at else "unknown"
        daily_pnl[day] = daily_pnl.get(day, 0) + t.pnl_dollars

    if len(daily_pnl) < 2:
        return 0.0

    returns = list(daily_pnl.values())
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance) if variance > 0 else 0

    if std == 0:
        return 0.0

    # Annualize: 252 trading days
    daily_rf = RISK_FREE_RATE / 252
    sharpe = (avg - daily_rf) / std * math.sqrt(252)
    return round(sharpe, 2)


def get_avg_hold_hours(days: int = 30) -> float:
    """Average hold duration in hours."""
    trades = _get_trades(days)
    if not trades:
        return 0.0
    total = sum(t.hold_duration_hours or 0 for t in trades)
    return round(total / len(trades), 1)


def get_daily_pnl(days: int = 30) -> list[dict]:
    """Daily P&L series for charting."""
    trades = _get_trades(days)
    daily: dict[str, float] = {}
    for t in trades:
        day = t.closed_at.strftime("%Y-%m-%d") if t.closed_at else "unknown"
        daily[day] = daily.get(day, 0) + t.pnl_dollars

    return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(daily.items())]


def get_performance_summary(days: int = 30) -> dict:
    """Combined performance summary."""
    trades = _get_trades(days)
    total_trades = len(trades)
    total_pnl = sum(t.pnl_dollars for t in trades)

    return {
        "period_days": days,
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(get_win_rate(days), 4),
        "profit_factor": round(get_profit_factor(days), 2) if get_profit_factor(days) != float("inf") else "inf",
        "max_drawdown": round(get_max_drawdown(days), 4),
        "sharpe_ratio": get_sharpe_ratio(days),
        "avg_hold_hours": get_avg_hold_hours(days),
        "daily_pnl": get_daily_pnl(days),
    }
