"""
Position reconciliation between Alpaca broker and local database.

Runs periodically to detect:
  - Orphans: positions in Alpaca but not in DB (adopted into DB, operator alerted)
  - Phantoms: positions in DB but not in Alpaca (marked CLOSED, operator alerted)
  - Price drift: >10% discrepancy between broker and DB (DB updated, warning logged)
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.logger import get_logger
from data.models import (
    PositionRecord,
    PositionStatus,
    SignalAction,
    get_session,
)
from services.alpaca_broker import get_broker
from services.telegram import TelegramNotifier

log = get_logger("reconciler")

PRICE_DRIFT_THRESHOLD = 0.10  # 10%


async def reconcile_positions() -> dict:
    """Compare broker positions against DB and fix discrepancies.

    Returns summary dict with counts of orphans, phantoms, and drift corrections.
    """
    broker = get_broker()
    notifier = TelegramNotifier()
    session = get_session()

    try:
        # Fetch from both sources
        broker_positions = broker.get_positions()
        broker_map = {p.option_symbol: p for p in broker_positions}

        db_positions = (
            session.query(PositionRecord)
            .filter(PositionRecord.status == PositionStatus.OPEN)
            .all()
        )
        db_map = {p.option_symbol: p for p in db_positions}

        orphans = 0
        phantoms = 0
        drift_fixes = 0

        # Check for orphans (in Alpaca, not in DB)
        for symbol, broker_pos in broker_map.items():
            if symbol not in db_map:
                orphans += 1
                log.warning(
                    "orphan_position_found",
                    symbol=symbol,
                    ticker=broker_pos.ticker,
                    qty=broker_pos.quantity,
                    entry_price=broker_pos.entry_price,
                )
                # Adopt into DB
                new_pos = PositionRecord(
                    position_id=f"orphan-{broker_pos.position_id[:12]}",
                    signal_id=f"unknown-{symbol}",
                    ticker=broker_pos.ticker,
                    option_symbol=symbol,
                    action=broker_pos.action,
                    strike=broker_pos.strike,
                    expiration=broker_pos.expiration,
                    quantity=broker_pos.quantity,
                    entry_price=broker_pos.entry_price,
                    entry_value=broker_pos.entry_price * broker_pos.quantity * 100,
                    current_price=broker_pos.current_price,
                    status=PositionStatus.OPEN,
                    entry_thesis="Adopted from broker — orphan position",
                )
                session.add(new_pos)
                await notifier.send(
                    f"<b>Orphan Position Found</b>\n"
                    f"Symbol: {symbol}\n"
                    f"Qty: {broker_pos.quantity} @ ${broker_pos.entry_price:.2f}\n"
                    f"Adopted into DB as {new_pos.position_id}"
                )

        # Check for phantoms (in DB, not in Alpaca)
        for symbol, db_pos in db_map.items():
            if symbol not in broker_map:
                phantoms += 1
                log.warning(
                    "phantom_position_found",
                    symbol=symbol,
                    ticker=db_pos.ticker,
                    position_id=db_pos.position_id,
                )
                db_pos.status = PositionStatus.CLOSED
                db_pos.closed_at = datetime.now(timezone.utc)
                await notifier.send(
                    f"<b>Phantom Position Closed</b>\n"
                    f"Symbol: {symbol} ({db_pos.ticker})\n"
                    f"Position {db_pos.position_id} marked CLOSED — not found in broker"
                )

        # Check for price drift on matching positions
        for symbol in set(broker_map.keys()) & set(db_map.keys()):
            broker_pos = broker_map[symbol]
            db_pos = db_map[symbol]

            if db_pos.current_price and db_pos.current_price > 0 and broker_pos.current_price > 0:
                drift = abs(broker_pos.current_price - db_pos.current_price) / db_pos.current_price
                if drift > PRICE_DRIFT_THRESHOLD:
                    drift_fixes += 1
                    log.warning(
                        "price_drift_detected",
                        symbol=symbol,
                        db_price=db_pos.current_price,
                        broker_price=broker_pos.current_price,
                        drift_pct=f"{drift:.1%}",
                    )
                    db_pos.current_price = broker_pos.current_price
                    db_pos.current_value = broker_pos.current_price * db_pos.quantity * 100

            # Always update DB with latest broker price and P&L
            db_pos.current_price = broker_pos.current_price
            db_pos.current_value = broker_pos.current_price * db_pos.quantity * 100
            if db_pos.entry_price and db_pos.entry_price > 0:
                db_pos.pnl_pct = ((broker_pos.current_price - db_pos.entry_price) / db_pos.entry_price) * 100
                db_pos.pnl_dollars = (broker_pos.current_price - db_pos.entry_price) * db_pos.quantity * 100
            db_pos.last_checked = datetime.now(timezone.utc)

        session.commit()

        summary = {
            "broker_positions": len(broker_map),
            "db_positions": len(db_map),
            "orphans_adopted": orphans,
            "phantoms_closed": phantoms,
            "drift_corrections": drift_fixes,
        }

        if orphans or phantoms or drift_fixes:
            log.info("reconciliation_actions", **summary)
        else:
            log.debug("reconciliation_clean", **summary)

        return summary

    except Exception as e:
        session.rollback()
        log.error("reconciliation_error", error=str(e))
        return {"error": str(e)}
    finally:
        session.close()
