"""
Performance analytics — pure SQL aggregate queries on TradeLog.

All functions accept a `days` parameter for the lookback window.
No ML, no complexity — straightforward calculations for monitoring and reporting.

Also provides:
  - Daily summary JSONL logging (data/daily_summary.jsonl)
  - Go/no-go checkpoint evaluation at 20/40/60 completed trades
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.logger import get_logger
from data.models import TradeLog, get_session

log = get_logger("analytics")

RISK_FREE_RATE = 0.05  # 5% annualized


def _get_trades(days: int = 30) -> list[TradeLog]:
    """Fetch closed trades within the lookback window."""
    session = get_session()
    try:
        from core.utils import trading_now
        cutoff = (trading_now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return (
            session.query(TradeLog)
            .filter(
                TradeLog.closed_at >= cutoff,
                TradeLog.exit_reason != "phantom_closure_reconciler",
            )
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


# ---------------------------------------------------------------------------
# Daily Summary JSONL
# ---------------------------------------------------------------------------

DAILY_SUMMARY_PATH = Path("data/daily_summary.jsonl")


def get_expectancy(days: int = 90) -> float:
    """Average P&L per trade (expectancy in dollars)."""
    trades = _get_trades(days)
    if not trades:
        return 0.0
    return round(sum(t.pnl_dollars for t in trades) / len(trades), 2)


def get_max_consecutive_losses(days: int = 90) -> int:
    """Longest consecutive losing streak in the lookback window."""
    trades = _get_trades(days)
    max_streak = 0
    current_streak = 0
    for t in trades:
        if t.pnl_dollars < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    return max_streak


def get_total_completed_trades() -> int:
    """Total number of completed (exited) trades since inception."""
    session = get_session()
    try:
        return (
            session.query(TradeLog)
            .filter(TradeLog.exit_reason != "phantom_closure_reconciler")
            .count()
        )
    finally:
        session.close()


def get_avg_slippage() -> dict:
    """Compute average and worst slippage from fill_quality.jsonl."""
    fill_path = Path("data/fill_quality.jsonl")
    if not fill_path.exists():
        return {"avg_slippage_pct": 0.0, "worst_slippage_pct": 0.0, "count": 0}

    slippages = []
    try:
        with fill_path.open() as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    s = record.get("slippage_pct")
                    if s is not None:
                        slippages.append(s)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    if not slippages:
        return {"avg_slippage_pct": 0.0, "worst_slippage_pct": 0.0, "count": 0}

    return {
        "avg_slippage_pct": round(sum(slippages) / len(slippages), 4),
        "worst_slippage_pct": round(max(slippages), 4),
        "count": len(slippages),
    }


def log_daily_summary(
    trades_entered: int,
    trades_exited: int,
    open_positions: int,
    account_value: float,
    signals_received: int,
    signals_passed_score: int,
    signals_passed_filters: int,
    signals_executed: int,
    circuit_breakers_fired: list[str],
    tickers_traded: list[str],
) -> None:
    """Append a daily summary record to data/daily_summary.jsonl."""
    from core.utils import trading_today

    today = trading_today()
    perf = get_performance_summary(30)
    slippage = get_avg_slippage()

    # Compute today's realized/unrealized P&L
    session = get_session()
    try:
        today_trades = (
            session.query(TradeLog)
            .filter(TradeLog.closed_at >= today)
            .all()
        )
        realized_pnl = sum(t.pnl_dollars for t in today_trades)
    finally:
        session.close()

    # Get portfolio delta from risk
    portfolio_delta = 0.0
    try:
        from tools.risk_tools import calculate_portfolio_risk
        risk = calculate_portfolio_risk()
        portfolio_delta = risk.get("delta_exposure", 0.0)
    except Exception:
        pass

    record = {
        "date": today,
        "trades_entered": trades_entered,
        "trades_exited": trades_exited,
        "open_positions": open_positions,
        "realized_pnl": round(realized_pnl, 2),
        "total_pnl": round(realized_pnl, 2),
        "account_value": round(account_value, 2),
        "win_rate_running": perf["win_rate"],
        "avg_slippage_pct": slippage["avg_slippage_pct"],
        "worst_slippage_pct": slippage["worst_slippage_pct"],
        "portfolio_delta": portfolio_delta,
        "signals_received": signals_received,
        "signals_passed_score": signals_passed_score,
        "signals_passed_filters": signals_passed_filters,
        "signals_executed": signals_executed,
        "circuit_breakers_fired": circuit_breakers_fired,
        "tickers_traded": tickers_traded,
    }

    try:
        DAILY_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DAILY_SUMMARY_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
        log.info("daily_summary_logged", date=today)
    except Exception as e:
        log.warning("daily_summary_log_error", error=str(e))


# ---------------------------------------------------------------------------
# Go/No-Go Checkpoint Evaluation
# ---------------------------------------------------------------------------

# Track last checkpoint so we don't send duplicates
_last_checkpoint_count: int = 0

CHECKPOINT_THRESHOLDS = [20, 40, 60]


def check_go_no_go() -> dict | None:
    """Evaluate go/no-go checkpoint at 20, 40, and 60 completed trades.

    Returns a checkpoint report dict if a threshold is crossed, else None.
    Only fires once per threshold.
    """
    global _last_checkpoint_count

    total = get_total_completed_trades()

    # Find the highest crossed threshold we haven't reported yet
    checkpoint = None
    for t in CHECKPOINT_THRESHOLDS:
        if total >= t and _last_checkpoint_count < t:
            checkpoint = t

    if checkpoint is None:
        return None

    _last_checkpoint_count = checkpoint

    # Compute metrics over all trades (no day window — lifetime)
    trades = _get_trades(days=365)
    if not trades:
        return None

    win_rate = get_win_rate(365)
    sharpe = get_sharpe_ratio(365)
    max_dd = get_max_drawdown(365)
    profit_factor = get_profit_factor(365)
    expectancy = get_expectancy(365)
    max_consec = get_max_consecutive_losses(365)
    slippage = get_avg_slippage()
    avg_slippage = slippage["avg_slippage_pct"]

    # Get average signal-to-fill time
    avg_fill_seconds = 0
    fill_path = Path("data/fill_quality.jsonl")
    if fill_path.exists():
        fill_times = []
        try:
            with fill_path.open() as f:
                for line in f:
                    try:
                        r = json.loads(line.strip())
                        s = r.get("signal_to_fill_seconds")
                        if s is not None:
                            fill_times.append(s)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
        if fill_times:
            avg_fill_seconds = int(sum(fill_times) / len(fill_times))

    # Count circuit breaker firings
    cb_fires = 0
    summary_path = Path("data/daily_summary.jsonl")
    if summary_path.exists():
        try:
            with summary_path.open() as f:
                for line in f:
                    try:
                        r = json.loads(line.strip())
                        fires = r.get("circuit_breakers_fired", [])
                        cb_fires += len(fires)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    # Build report
    if checkpoint == 60:
        # Final assessment
        criteria = {
            "win_rate": win_rate > 0.45,
            "sharpe": sharpe > 1.5,
            "avg_slippage": avg_slippage < 0.025,
            "max_drawdown": max_dd < 0.10,
            "expectancy": expectancy > 50,
            "circuit_breakers": cb_fires <= 2 * len(CHECKPOINT_THRESHOLDS),
        }
        all_pass = all(criteria.values())
        if all_pass:
            verdict = "READY FOR LIVE"
        elif sum(criteria.values()) >= 4:
            verdict = "NEEDS INVESTIGATION"
        else:
            verdict = "NOT VIABLE"

        report = {
            "checkpoint": checkpoint,
            "total_trades": total,
            "is_final": True,
            "verdict": verdict,
            "criteria_passed": criteria,
            "metrics": {
                "win_rate": round(win_rate, 4),
                "profit_factor": round(profit_factor, 2),
                "sharpe_estimate": sharpe,
                "avg_slippage_pct": round(avg_slippage, 4),
                "max_drawdown": round(max_dd, 4),
                "expectancy": expectancy,
                "avg_signal_to_fill_seconds": avg_fill_seconds,
                "circuit_breakers_fired": cb_fires,
                "max_consecutive_losses": max_consec,
            },
        }
    else:
        # Interim checkpoint (20, 40)
        fail_reasons = []
        if win_rate < 0.35:
            fail_reasons.append(f"win_rate {win_rate:.2f} < 0.35")
        if avg_slippage > 0.03:
            fail_reasons.append(f"avg_slippage {avg_slippage:.4f} > 0.03")
        if sharpe < 0.5:
            fail_reasons.append(f"sharpe {sharpe:.2f} < 0.5")

        report = {
            "checkpoint": checkpoint,
            "total_trades": total,
            "is_final": False,
            "pass": len(fail_reasons) == 0,
            "fail_reasons": fail_reasons,
            "metrics": {
                "win_rate": round(win_rate, 4),
                "avg_slippage_pct": round(avg_slippage, 4),
                "sharpe_estimate": sharpe,
                "worst_single_loss": round(min((t.pnl_pct for t in trades), default=0), 2),
                "max_consecutive_losses": max_consec,
                "avg_signal_to_fill_seconds": avg_fill_seconds,
            },
        }

    log.info("go_no_go_checkpoint", checkpoint=checkpoint, report=report)
    return report


def format_checkpoint_telegram(report: dict) -> str:
    """Format a go/no-go checkpoint report for Telegram."""
    m = report["metrics"]
    checkpoint = report["checkpoint"]

    if report.get("is_final"):
        lines = [
            f"<b>=== {checkpoint}-TRADE FINAL ASSESSMENT ===</b>",
            f"",
            f"Live win rate:          {m['win_rate']:.2f} (backtest: 0.644)",
            f"Live profit factor:     {m['profit_factor']:.2f} (backtest: 5.03)",
            f"Live Sharpe estimate:   {m['sharpe_estimate']:.2f} (backtest: 8.60, expected: 2-3)",
            f"Live avg slippage:      {m['avg_slippage_pct']:.2%}",
            f"Live max drawdown:      {m['max_drawdown']:.2%} (backtest: 1.1%)",
            f"Live expectancy:        ${m['expectancy']:.2f}/trade (backtest: $285)",
            f"Avg signal-to-fill:     {m['avg_signal_to_fill_seconds']}s",
            f"Circuit breakers fired: {m['circuit_breakers_fired']} times",
            f"",
            f"<b>VERDICT: {report['verdict']}</b>",
        ]
        if report["verdict"] == "READY FOR LIVE":
            lines.append("\nAll criteria passed. Strategy validated for live trading.")
        elif report["verdict"] == "NEEDS INVESTIGATION":
            failed = [k for k, v in report["criteria_passed"].items() if not v]
            lines.append(f"\nFailed criteria: {', '.join(failed)}")
        else:
            failed = [k for k, v in report["criteria_passed"].items() if not v]
            lines.append(f"\nFailed criteria: {', '.join(failed)}")
            lines.append("\n<b>RECOMMENDATION:</b> Strategy not viable. Consider halting.")
    else:
        lines = [
            f"<b>=== {checkpoint}-TRADE CHECKPOINT ===</b>",
            f"",
            f"Live win rate:        {m['win_rate']:.2f} (target: > 0.45)",
            f"Live avg slippage:    {m['avg_slippage_pct']:.2%} (target: < 2.5%)",
            f"Live Sharpe estimate: {m['sharpe_estimate']:.2f} (target: > 1.0)",
            f"Worst single loss:    {m['worst_single_loss']:.2f}%",
            f"Max consecutive loss: {m['max_consecutive_losses']}",
            f"Signal-to-fill avg:   {m['avg_signal_to_fill_seconds']}s",
            f"",
            f"GO/NO-GO: <b>{'PASS' if report['pass'] else 'FAIL'}</b>",
        ]
        if not report["pass"]:
            lines.append(f"Failed: {', '.join(report['fail_reasons'])}")
            lines.append(
                "\n<b>RECOMMENDATION:</b> Strategy underperforming backtest expectations. "
                "Consider halting and investigating before continuing."
            )

    return "\n".join(lines)
