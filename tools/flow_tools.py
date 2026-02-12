"""
Tools for the Flow Scanner subagent.

These functions are exposed as Claude tool_use definitions so the
flow_scanner agent can call them to fetch and score options flow data.
"""
from __future__ import annotations

from datetime import datetime, date, timezone

from config.settings import get_settings
from core.utils import TZ
from core.logger import get_logger
from data.models import FlowSignal, SignalAction, SignalRecord, get_session
from services.unusual_whales import UnusualWhalesClient

log = get_logger("flow_tools")

# Accumulate scored signals per scan cycle for Telegram digest
_scan_scored_signals: list[tuple[dict, dict]] = []  # (signal, score_result)

# High-watermark: track contracts already scored today so we don't rescore
# the same contract (e.g. FI CALL $230 2026-03-20) on every poll cycle.
# Keyed by "TICKER:TYPE:STRIKE:EXPIRATION", resets each trading day.
_seen_contracts: set[str] = set()
_seen_date: str = ""  # YYYY-MM-DD in ET, used to reset daily


def _contract_key(sig: FlowSignal) -> str:
    """Build a unique fingerprint for a contract."""
    return f"{sig.ticker}:{sig.option_type}:{sig.strike}:{sig.expiration}"


async def scan_flow() -> list[dict]:
    """Fetch recent unusual options flow and return filtered signals.

    Maintains a high-watermark set of already-scored contracts so the same
    contract is not re-scored on subsequent poll cycles within the same day.
    Returns a list of *new* signal dicts ready for AI scoring.
    """
    global _seen_date

    # Reset seen set at the start of each trading day (PT)
    today_tz = datetime.now(TZ).strftime("%Y-%m-%d")
    if today_tz != _seen_date:
        _seen_contracts.clear()
        _seen_date = today_tz
        log.info("seen_contracts_reset", date=today_tz)

    # Clear previous cycle's scored signals
    _scan_scored_signals.clear()

    client = UnusualWhalesClient()
    signals = await client.fetch_flow()

    results = []
    skipped = 0
    for sig in signals[: get_settings().flow.max_analyze]:
        key = _contract_key(sig)
        if key in _seen_contracts:
            skipped += 1
            continue
        _seen_contracts.add(key)
        results.append(sig.model_dump())

    log.info(
        "scan_flow_complete",
        signals_returned=len(results),
        skipped_already_seen=skipped,
        seen_total=len(_seen_contracts),
    )
    return results


def score_signal(signal: dict) -> dict:
    """Score a signal on a 0-10 scale based on reward/penalty indicators.

    Args:
        signal: FlowSignal dict with ticker, premium, vol_oi_ratio, etc.

    Returns:
        Dict with score, breakdown, and pass/fail status.
    """
    settings = get_settings()
    score = 0
    breakdown: list[str] = []

    # --- Reward indicators ---
    order_type = signal.get("order_type", "").lower()
    if "sweep" in order_type:
        score += 2
        breakdown.append("sweep:+2")
    if "floor" in order_type:
        score += 2
        breakdown.append("floor:+2")

    # Opening position
    if signal.get("vol_oi_ratio", 0) >= 1.5:
        score += 1
        breakdown.append("vol_oi>=1.5:+1")
    if signal.get("vol_oi_ratio", 0) >= 3.0:
        score += 1
        breakdown.append("vol_oi>=3.0:+1")

    # Premium size
    premium = signal.get("premium", 0)
    if premium >= 500_000:
        score += 2
        breakdown.append("premium>=500K:+2")
    elif premium >= 250_000:
        score += 1
        breakdown.append("premium>=250K:+1")

    # Opening trade bonus
    if "open" in order_type:
        score += 2
        breakdown.append("opening:+2")

    # Directional conviction
    directional_pct = signal.get("directional_pct", 0)
    directional_side = signal.get("directional_side", "")
    if directional_pct >= 0.90:
        score += 2
        breakdown.append(f"direction>=90%({directional_side}):+2")
    elif directional_pct >= 0.75:
        score += 1
        breakdown.append(f"direction>=75%({directional_side}):+1")

    # Single leg bonus (institutional conviction)
    if signal.get("has_singleleg") and not signal.get("has_multileg"):
        score += 1
        breakdown.append("singleleg:+1")

    # Low trade count (institutional block)
    trade_count = signal.get("trade_count", 0)
    if trade_count > 0 and trade_count < 10:
        score += 1
        breakdown.append(f"block({trade_count}trades):+1")

    # --- Penalty indicators ---
    iv_rank = signal.get("iv_rank", 0)
    if iv_rank > 70:
        score -= 3
        breakdown.append(f"iv_rank>{70}:-3")

    dte = signal.get("dte", 0)
    if dte < 7:
        score -= 2
        breakdown.append("dte<7:-2")
    elif dte < 14:
        score -= 1
        breakdown.append("dte<14:-1")

    # Near earnings penalty
    next_earnings = signal.get("next_earnings_date", "")
    if next_earnings:
        try:
            earnings_date = date.fromisoformat(next_earnings)
            days_to_earnings = (earnings_date - datetime.now(TZ).date()).days
            if 0 <= days_to_earnings < 7:
                score -= 2
                breakdown.append(f"earnings_in_{days_to_earnings}d:-2")
        except (ValueError, TypeError):
            pass

    # Clamp to 0-10
    score = max(0, min(10, score))

    passed = score >= settings.flow.min_score
    result = {
        "signal_id": signal.get("signal_id", ""),
        "ticker": signal.get("ticker", ""),
        "score": score,
        "breakdown": ", ".join(breakdown),
        "passed": passed,
        "min_required": settings.flow.min_score,
    }

    if directional_pct > 0:
        log.info(
            "signal_scored",
            ticker=result["ticker"],
            score=score,
            passed=passed,
            direction=f"{directional_pct:.0%} {directional_side} side",
        )
    else:
        log.info("signal_scored", ticker=result["ticker"], score=score, passed=passed)

    # Accumulate for Telegram digest
    _scan_scored_signals.append((signal, result))

    return result


def save_signal(signal: dict, score_result: dict) -> str:
    """Persist a scored signal to the database.

    Returns the signal_id.
    """
    session = get_session()
    try:
        record = SignalRecord(
            signal_id=signal.get("signal_id", ""),
            ticker=signal.get("ticker", ""),
            action=SignalAction(signal.get("action", "CALL")),
            strike=signal.get("strike", 0),
            expiration=signal.get("expiration", ""),
            premium=signal.get("premium", 0),
            volume=signal.get("volume", 0),
            open_interest=signal.get("open_interest", 0),
            vol_oi_ratio=signal.get("vol_oi_ratio", 0),
            option_type=signal.get("option_type", ""),
            order_type=signal.get("order_type", ""),
            score=score_result.get("score", 0),
            score_breakdown=score_result.get("breakdown", ""),
            underlying_price=signal.get("underlying_price", 0),
            iv_rank=signal.get("iv_rank", 0),
            dte=signal.get("dte", 0),
            accepted=score_result.get("passed", False),
            reject_reason="" if score_result.get("passed") else f"score={score_result.get('score', 0)}<{score_result.get('min_required', 7)}",
        )
        session.add(record)
        session.commit()
        log.info("signal_saved", signal_id=record.signal_id, accepted=record.accepted)
        return record.signal_id
    except Exception as e:
        session.rollback()
        log.error("signal_save_error", error=str(e))
        raise
    finally:
        session.close()


async def send_scan_report() -> dict:
    """Send a Telegram digest of all scored signals from this scan cycle.

    Called at the end of each scan cycle to notify the user of what was
    found, scored, and why signals passed or failed.
    """
    from services.telegram import TelegramNotifier

    scored = list(_scan_scored_signals)
    if not scored:
        return {"sent": False, "reason": "no signals scored this cycle"}

    notifier = TelegramNotifier()

    passed = [(s, r) for s, r in scored if r["passed"]]
    failed = [(s, r) for s, r in scored if not r["passed"]]

    lines = [f"<b>Flow Scan Report</b>  ({len(scored)} scored)"]

    if passed:
        lines.append(f"\n<b>{len(passed)} PASSED</b> (score >= 7):")
        for sig, res in passed:
            ticker = sig.get("ticker", "?")
            opt = sig.get("option_type", "?")
            strike = sig.get("strike", 0)
            dte = sig.get("dte", 0)
            prem = sig.get("premium", 0)
            dir_pct = sig.get("directional_pct", 0)
            dir_side = sig.get("directional_side", "")
            dir_str = f" {dir_pct:.0%} {dir_side}" if dir_pct > 0 else ""
            lines.append(
                f"  <b>{ticker}</b> {opt} ${strike:.0f}  DTE {dte}\n"
                f"    ${prem:,.0f}  Score <b>{res['score']}/10</b>{dir_str}\n"
                f"    <i>{res['breakdown']}</i>"
            )

    if failed:
        lines.append(f"\n{len(failed)} rejected:")
        for sig, res in failed:
            ticker = sig.get("ticker", "?")
            opt = sig.get("option_type", "?")
            strike = sig.get("strike", 0)
            prem = sig.get("premium", 0)
            lines.append(f"  {ticker} {opt} ${strike:.0f}  ${prem:,.0f}  Score {res['score']}/10")

    text = "\n".join(lines)
    sent = await notifier.send(text)
    log.info("scan_report_sent", signals=len(scored), passed=len(passed), failed=len(failed))
    return {"sent": sent, "signals": len(scored), "passed": len(passed), "failed": len(failed)}


# ---------------------------------------------------------------------------
# Tool definitions for Claude agent SDK
# ---------------------------------------------------------------------------

FLOW_TOOLS = [
    {
        "name": "scan_flow",
        "description": (
            "Fetch recent unusual options flow from Unusual Whales API. "
            "Returns a list of filtered signal dicts with ticker, strike, premium, "
            "volume, open_interest, vol_oi_ratio, option_type, order_type, dte, iv_rank."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "score_signal",
        "description": (
            "Score a flow signal on a 0-10 scale. Evaluates sweep/floor type, "
            "volume/OI ratio, premium size, directional conviction (ask/bid side), "
            "single-leg structure, trade count, IV rank, DTE, and earnings proximity. "
            "Returns score, breakdown, and whether it passes the minimum threshold (7+)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal": {
                    "type": "object",
                    "description": "The signal dict from scan_flow to score",
                },
            },
            "required": ["signal"],
        },
    },
    {
        "name": "save_signal",
        "description": "Save a scored signal to the database for record-keeping.",
        "input_schema": {
            "type": "object",
            "properties": {
                "signal": {
                    "type": "object",
                    "description": "The original signal dict",
                },
                "score_result": {
                    "type": "object",
                    "description": "The scoring result from score_signal",
                },
            },
            "required": ["signal", "score_result"],
        },
    },
    {
        "name": "send_scan_report",
        "description": (
            "Send a Telegram digest of all signals scored in this scan cycle. "
            "Call this ONCE at the end of every scan cycle after all signals have been "
            "scored and saved. Shows what was found, scores, and pass/fail reasons."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
