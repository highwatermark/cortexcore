"""
Tools for the Flow Scanner subagent.

These functions are exposed as Claude tool_use definitions so the
flow_scanner agent can call them to fetch and score options flow data.
"""
from __future__ import annotations

from datetime import datetime, timezone

from config.settings import get_settings
from core.logger import get_logger
from data.models import FlowSignal, SignalAction, SignalRecord, get_session
from services.unusual_whales import UnusualWhalesClient

log = get_logger("flow_tools")


async def scan_flow() -> list[dict]:
    """Fetch recent unusual options flow and return filtered signals.

    Returns a list of signal dicts ready for AI scoring.
    """
    client = UnusualWhalesClient()
    signals = await client.fetch_flow()

    results = []
    for sig in signals[: get_settings().flow.max_analyze]:
        results.append(sig.model_dump())

    log.info("scan_flow_complete", signals_returned=len(results))
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

    log.info("signal_scored", ticker=result["ticker"], score=score, passed=passed)
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
            "volume/OI ratio, premium size, IV rank, and DTE. Returns score, "
            "breakdown, and whether it passes the minimum threshold (7+)."
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
]
