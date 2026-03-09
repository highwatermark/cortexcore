"""
Position reconciliation between Alpaca broker and local database.

Runs periodically to detect:
  - Orphans: positions in Alpaca but not in DB (adopted into DB, operator alerted)
  - Phantoms: positions in DB but not in Alpaca (marked CLOSED, operator alerted)
  - Price drift: >10% discrepancy between broker and DB (DB updated, warning logged)

Safety guards:
  - Phantom closure checks for pending exit orders before closing
  - Phantom closure checks for existing TradeLog (already closed by execute_exit)
  - Orphan adoption skips symbols recently closed (within RECENT_CLOSE_WINDOW)
  - Orphan adoption skips symbols phantom-closed in same cycle
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.logger import get_logger
from data.models import (
    BrokerOrder,
    IntentStatus,
    OrderIntent,
    OrderSide,
    OrderStatus,
    PositionRecord,
    PositionStatus,
    TradeLog,
    get_session,
)
from services.alpaca_broker import get_broker
from services.telegram import TelegramNotifier

log = get_logger("reconciler")

PRICE_DRIFT_THRESHOLD = 0.10  # 10%
RECENT_CLOSE_WINDOW = timedelta(minutes=30)


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
        skipped_phantoms = 0
        skipped_orphans = 0
        phantom_closed_symbols: set[str] = set()

        # --- PHASE 1: Phantom detection (in DB but not in Alpaca) ---
        # Run BEFORE orphan detection so we can block re-adoption of same symbol.
        for symbol, db_pos in db_map.items():
            if symbol in broker_map:
                continue

            # GUARD 1: Check if a TradeLog already exists for this position.
            # If execute_exit() already closed it, the DB status may not have
            # propagated yet (race), or the position was already handled.
            existing_trade = (
                session.query(TradeLog)
                .filter(TradeLog.position_id == db_pos.position_id)
                .first()
            )
            if existing_trade:
                # Already has a trade log — just mark position closed, no duplicate log
                log.info(
                    "phantom_already_closed",
                    position_id=db_pos.position_id,
                    ticker=db_pos.ticker,
                    trade_log_id=existing_trade.id,
                )
                db_pos.status = PositionStatus.CLOSED
                db_pos.closed_at = db_pos.closed_at or datetime.now(timezone.utc)
                skipped_phantoms += 1
                phantom_closed_symbols.add(symbol)
                continue

            # GUARD 2: Check for a pending exit order (exit in flight).
            # If execute_exit() submitted a sell order that hasn't filled yet,
            # don't phantom-close — the order reconciler will handle the fill.
            exit_intent_key = f"exit-{db_pos.position_id}"
            pending_exit = (
                session.query(OrderIntent)
                .filter(
                    OrderIntent.idempotency_key == exit_intent_key,
                    OrderIntent.status == IntentStatus.PENDING,
                )
                .first()
            )
            if pending_exit:
                log.info(
                    "phantom_skipped_exit_pending",
                    position_id=db_pos.position_id,
                    ticker=db_pos.ticker,
                    intent_key=exit_intent_key,
                )
                skipped_phantoms += 1
                continue

            # GUARD 3: Check for a submitted/pending SELL order at the broker.
            # The order may be working but position already gone from broker positions.
            pending_sell = (
                session.query(BrokerOrder)
                .filter(
                    BrokerOrder.option_symbol == symbol,
                    BrokerOrder.side == OrderSide.SELL,
                    BrokerOrder.status.in_([
                        OrderStatus.SUBMITTED,
                        OrderStatus.PENDING,
                        OrderStatus.PARTIAL,
                    ]),
                )
                .first()
            )
            if pending_sell:
                log.info(
                    "phantom_skipped_sell_order_working",
                    position_id=db_pos.position_id,
                    ticker=db_pos.ticker,
                    broker_order_id=pending_sell.broker_order_id,
                )
                skipped_phantoms += 1
                continue

            # No guards triggered — this is a genuine phantom.
            phantoms += 1
            phantom_closed_symbols.add(symbol)
            log.warning(
                "phantom_position_found",
                symbol=symbol,
                ticker=db_pos.ticker,
                position_id=db_pos.position_id,
            )
            db_pos.status = PositionStatus.CLOSED
            db_pos.closed_at = datetime.now(timezone.utc)

            # NOTE: No TradeLog created for phantom closures.
            # Phantom P&L uses stale DB prices (unreliable), and phantom entries
            # were poisoning circuit breakers and performance analytics.
            # Real exits go through execute_exit() which creates accurate TradeLog entries.

            await notifier.send(
                f"<b>Phantom Position Closed</b>\n"
                f"Symbol: {symbol} ({db_pos.ticker})\n"
                f"Position {db_pos.position_id} marked CLOSED — not found in broker"
            )

        # --- PHASE 2: Orphan detection (in Alpaca but not in DB) ---
        for symbol, broker_pos in broker_map.items():
            if symbol in db_map:
                continue

            # GUARD 1: Don't re-adopt a symbol that was just phantom-closed
            # in this same reconciliation cycle.
            if symbol in phantom_closed_symbols:
                log.info(
                    "orphan_skipped_just_phantom_closed",
                    symbol=symbol,
                    ticker=broker_pos.ticker,
                )
                skipped_orphans += 1
                continue

            # GUARD 2: Don't adopt a symbol that was ABANDONED.
            # Abandoned positions are illiquid/worthless — re-adopting would
            # restart the infinite exit-retry loop.
            abandoned = (
                session.query(PositionRecord)
                .filter(
                    PositionRecord.option_symbol == symbol,
                    PositionRecord.status == PositionStatus.ABANDONED,
                )
                .first()
            )
            if abandoned:
                log.info(
                    "orphan_skipped_abandoned",
                    symbol=symbol,
                    ticker=broker_pos.ticker,
                    abandoned_position_id=abandoned.position_id,
                )
                skipped_orphans += 1
                continue

            # GUARD 3: Don't adopt a symbol that was recently closed.
            # This prevents the phantom→orphan→phantom death spiral.
            cutoff = datetime.now(timezone.utc) - RECENT_CLOSE_WINDOW
            recently_closed = (
                session.query(PositionRecord)
                .filter(
                    PositionRecord.option_symbol == symbol,
                    PositionRecord.status == PositionStatus.CLOSED,
                    PositionRecord.closed_at >= cutoff,
                )
                .first()
            )
            if recently_closed:
                log.info(
                    "orphan_skipped_recently_closed",
                    symbol=symbol,
                    ticker=broker_pos.ticker,
                    closed_position_id=recently_closed.position_id,
                    closed_at=str(recently_closed.closed_at),
                )
                skipped_orphans += 1
                continue

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

        # --- PHASE 3: Price drift on matching positions ---
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
                    log.debug(
                        "price_drift_corrected",
                        symbol=symbol,
                        new_price=broker_pos.current_price,
                        new_value=broker_pos.current_price * db_pos.quantity * 100,
                    )

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
            "skipped_phantoms": skipped_phantoms,
            "skipped_orphans": skipped_orphans,
        }

        if orphans or phantoms or drift_fixes or skipped_phantoms or skipped_orphans:
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
