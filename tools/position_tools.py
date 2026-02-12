"""
Tools for the Position Manager subagent.

Provides position queries, P&L calculations, and exit trigger detection.
"""
from __future__ import annotations

from datetime import datetime, timezone

from config.settings import get_settings
from core.utils import calc_dte
from core.logger import get_logger
from data.models import (
    PositionRecord,
    PositionSnapshot,
    PositionStatus,
    get_session,
)
from services.alpaca_broker import AlpacaBroker
from services.alpaca_options_data import get_options_data_client

log = get_logger("position_tools")


def get_open_positions() -> list[dict]:
    """Get all open positions with current P&L from both DB and broker.

    Returns list of position dicts with full details.
    """
    session = get_session()
    try:
        db_positions = (
            session.query(PositionRecord)
            .filter(PositionRecord.status == PositionStatus.OPEN)
            .all()
        )

        # Merge with live broker data
        broker = AlpacaBroker()
        broker_positions = broker.get_positions()
        broker_map = {p.option_symbol: p for p in broker_positions}

        results = []
        for pos in db_positions:
            live = broker_map.get(pos.option_symbol)
            current_price = live.current_price if live else (pos.current_price or pos.entry_price)
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else 0
            pnl_dollars = (current_price - pos.entry_price) * pos.quantity * 100  # options multiplier

            # Calculate remaining DTE (uses Pacific time via core.utils)
            dte_remaining = calc_dte(pos.expiration) if pos.expiration else 0

            results.append({
                "position_id": pos.position_id,
                "ticker": pos.ticker,
                "option_symbol": pos.option_symbol,
                "action": pos.action.value if pos.action else "CALL",
                "strike": pos.strike,
                "expiration": pos.expiration,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "current_price": round(current_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_dollars": round(pnl_dollars, 2),
                "delta": pos.delta or 0,
                "gamma": pos.gamma or 0,
                "theta": pos.theta or 0,
                "vega": pos.vega or 0,
                "conviction": pos.conviction,
                "dte_remaining": dte_remaining,
                "entry_thesis": pos.entry_thesis,
            })

        log.info("positions_fetched", count=len(results))
        return results
    finally:
        session.close()


def _get_adaptive_profit_target(dte: int) -> float:
    """Get DTE-appropriate profit target percentage.

    Closer to expiration = lower profit target (take profits sooner).
    """
    settings = get_settings()
    trading = settings.trading
    if dte > 14:
        return trading.adaptive_target_dte_gt_14
    elif dte > 7:
        return trading.adaptive_target_dte_7_to_14
    elif dte > 3:
        return trading.adaptive_target_dte_3_to_7
    else:
        return trading.adaptive_target_dte_lt_3


def check_exit_triggers(position: dict) -> dict:
    """Evaluate a position against exit triggers.

    Returns a dict with trigger analysis and recommended action.
    """
    settings = get_settings()
    risk = settings.risk
    trading = settings.trading
    triggers: list[str] = []
    should_exit = False
    urgency = "low"

    pnl_pct = position.get("pnl_pct", 0)
    dte = position.get("dte_remaining", 999)
    conviction = position.get("conviction", 100)

    # Mandatory DTE exit — highest priority, non-negotiable
    if dte <= trading.max_hold_dte:
        triggers.append(f"DTE_MANDATORY: DTE={dte} <= {trading.max_hold_dte} — mandatory exit regardless of P&L")
        should_exit = True
        urgency = "critical"

    # Adaptive profit target based on DTE
    profit_target = _get_adaptive_profit_target(dte)
    if pnl_pct >= profit_target * 100:
        triggers.append(f"PROFIT_TARGET: P&L {pnl_pct:.1f}% >= {profit_target * 100:.0f}% (DTE-adjusted)")
        should_exit = True
        urgency = max(urgency, "high", key=lambda x: ["low", "medium", "high", "critical"].index(x))

    # Hard stop loss
    if pnl_pct <= -risk.stop_loss_pct * 100:
        triggers.append(f"STOP_LOSS: P&L {pnl_pct:.1f}% <= -{risk.stop_loss_pct * 100}%")
        should_exit = True
        urgency = "critical"

    # Gamma risk
    if dte <= risk.gamma_risk_dte_threshold and pnl_pct < 20:
        triggers.append(f"GAMMA_RISK: DTE={dte} <= {risk.gamma_risk_dte_threshold} with P&L {pnl_pct:.1f}%")
        should_exit = True
        urgency = max(urgency, "high", key=lambda x: ["low", "medium", "high", "critical"].index(x))

    # Conviction drop
    if conviction < risk.conviction_exit_threshold:
        triggers.append(f"CONVICTION_DROP: {conviction}% < {risk.conviction_exit_threshold}%")
        should_exit = True
        if urgency == "low":
            urgency = "medium"

    # Theta acceleration check
    theta = abs(position.get("theta", 0))
    entry_price = position.get("entry_price", 1)
    if entry_price > 0 and theta > 0:
        theta_pct = theta / entry_price
        if theta_pct > 0.05:  # daily decay > 5% of premium
            triggers.append(f"THETA_ACCEL: daily decay {theta_pct:.1%} of premium")
            if urgency == "low":
                urgency = "medium"

    result = {
        "position_id": position.get("position_id", ""),
        "ticker": position.get("ticker", ""),
        "triggers": triggers,
        "should_exit": should_exit,
        "urgency": urgency,
        "recommended_action": "EXIT" if should_exit else "HOLD",
    }

    if triggers:
        log.info("exit_triggers_found", ticker=result["ticker"], triggers=triggers, should_exit=should_exit)

    return result


def update_position_price(position_id: str, current_price: float) -> bool:
    """Update a position's current price and P&L in the database."""
    session = get_session()
    try:
        pos = session.query(PositionRecord).filter(PositionRecord.position_id == position_id).first()
        if not pos:
            return False

        pos.current_price = current_price
        pos.current_value = current_price * pos.quantity * 100
        pos.pnl_pct = ((current_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else 0
        pos.pnl_dollars = (current_price - pos.entry_price) * pos.quantity * 100
        pos.last_checked = datetime.now(timezone.utc)
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        log.error("position_update_error", position_id=position_id, error=str(e))
        return False
    finally:
        session.close()


def update_position_greeks(
    position_id: str,
    delta: float | None = None,
    gamma: float | None = None,
    theta: float | None = None,
    vega: float | None = None,
    iv: float | None = None,
) -> bool:
    """Update a position's Greeks and IV in the database."""
    session = get_session()
    try:
        pos = session.query(PositionRecord).filter(PositionRecord.position_id == position_id).first()
        if not pos:
            return False

        if delta is not None:
            pos.delta = delta
        if gamma is not None:
            pos.gamma = gamma
        if theta is not None:
            pos.theta = theta
        if vega is not None:
            pos.vega = vega
        if iv is not None:
            pos.iv = iv
        pos.last_checked = datetime.now(timezone.utc)
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        log.error("greeks_update_error", position_id=position_id, error=str(e))
        return False
    finally:
        session.close()


def refresh_positions() -> dict:
    """Fetch snapshots for all open positions and persist price, P&L, and Greeks.

    Makes a single batch API call via AlpacaOptionsData, then updates each
    position in the database.

    Returns summary dict with counts.
    """
    session = get_session()
    try:
        open_positions = (
            session.query(PositionRecord)
            .filter(PositionRecord.status == PositionStatus.OPEN)
            .all()
        )

        if not open_positions:
            return {"positions": 0, "updated": 0, "errors": 0}

        symbols = [p.option_symbol for p in open_positions]
        data_client = get_options_data_client()
        snapshots = data_client.get_snapshots(symbols)

        updated = 0
        errors = 0
        for pos in open_positions:
            snap = snapshots.get(pos.option_symbol)
            if not snap:
                continue

            try:
                price = snap["current_price"]
                pos.current_price = price
                pos.current_value = price * pos.quantity * 100
                if pos.entry_price and pos.entry_price > 0:
                    pos.pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100
                    pos.pnl_dollars = (price - pos.entry_price) * pos.quantity * 100

                if snap.get("delta") is not None:
                    pos.delta = snap["delta"]
                if snap.get("gamma") is not None:
                    pos.gamma = snap["gamma"]
                if snap.get("theta") is not None:
                    pos.theta = snap["theta"]
                if snap.get("vega") is not None:
                    pos.vega = snap["vega"]
                if snap.get("iv") is not None:
                    pos.iv = snap["iv"]

                pos.last_checked = datetime.now(timezone.utc)
                updated += 1
            except Exception as e:
                errors += 1
                log.error("position_refresh_error", position_id=pos.position_id, error=str(e))

        session.commit()

        summary = {"positions": len(open_positions), "updated": updated, "errors": errors}
        log.info("positions_refreshed", **summary)
        return summary
    except Exception as e:
        session.rollback()
        log.error("refresh_positions_failed", error=str(e))
        return {"positions": 0, "updated": 0, "errors": 0, "error": str(e)}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Tool definitions for Claude agent SDK
# ---------------------------------------------------------------------------

POSITION_TOOLS = [
    {
        "name": "get_open_positions",
        "description": (
            "Get all open options positions with current prices, P&L, Greeks, "
            "conviction scores, and remaining DTE. Merges database records with "
            "live broker data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "check_exit_triggers",
        "description": (
            "Evaluate a position against exit triggers: mandatory DTE exit (<=5 DTE), "
            "adaptive profit target (40%/35%/25%/15% based on DTE), stop loss (-35%), "
            "gamma risk (DTE<=5), conviction drop (<50%), and theta acceleration. "
            "Returns trigger list and recommended action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "object",
                    "description": "Position dict from get_open_positions",
                },
            },
            "required": ["position"],
        },
    },
]
