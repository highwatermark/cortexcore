"""
Tools for the Executor subagent.

Handles trade execution with idempotency, limit orders, fill polling, and notification.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from config.settings import get_settings
from core.logger import get_logger
from core.safety import get_safety_gate
from core.utils import parse_occ_symbol
from data.models import (
    BrokerOrder,
    IntentStatus,
    OrderIntent,
    OrderSide,
    OrderStatus,
    PositionRecord,
    PositionStatus,
    SignalAction,
    TradeLog,
    get_session,
)
from services.alpaca_broker import AlpacaBroker
from services.telegram import TelegramNotifier

log = get_logger("execution_tools")

# Max time to wait for a fill before returning (seconds)
FILL_POLL_TIMEOUT = 30
FILL_POLL_INTERVAL = 3


def calculate_position_size(option_price: float, equity: float | None = None) -> dict:
    """Calculate max contracts for a new position based on equity and limits.

    Considers three constraints (takes the minimum):
      1. max_per_trade_pct — 20% of equity per trade
      2. max_position_value — $1,000 absolute cap per position
      3. remaining total exposure capacity — 25% of equity minus existing positions

    Returns dict with max_contracts, limits breakdown, and limiting factor.
    """
    settings = get_settings()
    trading = settings.trading

    # Get equity if not provided
    if equity is None:
        try:
            broker = AlpacaBroker()
            account = broker.get_account()
            equity = account.get("equity", 0)
        except Exception:
            return {"max_contracts": 0, "error": "Could not fetch account equity"}

    if equity <= 0:
        return {"max_contracts": 0, "error": "No equity available"}
    if option_price <= 0:
        return {"max_contracts": 0, "error": "Invalid option price"}

    cost_per_contract = option_price * 100  # options multiplier

    # Limit 1: max_per_trade_pct (20% of equity)
    max_by_trade_pct = int((equity * trading.max_per_trade_pct) / cost_per_contract)

    # Limit 2: max_position_value ($1,000 absolute cap)
    max_by_position_value = int(trading.max_position_value / cost_per_contract)

    # Limit 3: remaining capacity under total exposure
    session = get_session()
    try:
        positions = (
            session.query(PositionRecord)
            .filter(PositionRecord.status == PositionStatus.OPEN)
            .all()
        )
        current_exposure = sum((p.entry_value or 0) for p in positions)
    finally:
        session.close()

    max_total_exposure = equity * trading.max_total_exposure_pct
    remaining_capacity = max_total_exposure - current_exposure
    max_by_exposure = int(remaining_capacity / cost_per_contract) if remaining_capacity > 0 else 0

    # Take the minimum of all limits (floor at 0)
    max_contracts = max(0, min(max_by_trade_pct, max_by_position_value, max_by_exposure))

    # Determine limiting factor
    limits = {
        "per_trade_pct": max_by_trade_pct,
        "position_value_cap": max_by_position_value,
        "total_exposure": max_by_exposure,
    }
    limiting_factor = min(limits, key=limits.get)

    return {
        "max_contracts": max_contracts,
        "option_price": option_price,
        "cost_per_contract": cost_per_contract,
        "equity": equity,
        "current_exposure": current_exposure,
        "remaining_capacity": round(remaining_capacity, 2),
        "limits": limits,
        "limiting_factor": limiting_factor,
    }


async def _wait_for_fill(broker: AlpacaBroker, order_id: str, timeout: int = FILL_POLL_TIMEOUT) -> dict:
    """Poll broker for order fill status.

    Returns order status dict with filled_qty, filled_avg_price, status.
    """
    elapsed = 0
    while elapsed < timeout:
        status = broker.get_order_status(order_id)
        order_status = status.get("status", "").lower()

        if order_status in ("filled",):
            return status
        if order_status in ("cancelled", "canceled", "expired", "rejected"):
            return status
        if status.get("filled_qty", 0) > 0 and order_status == "partially_filled":
            return status

        await asyncio.sleep(FILL_POLL_INTERVAL)
        elapsed += FILL_POLL_INTERVAL

    # Timeout — return last known status
    return broker.get_order_status(order_id)


async def execute_entry(
    signal_id: str,
    ticker: str,
    option_symbol: str,
    side: str,
    quantity: int,
    limit_price: float,
    thesis: str = "",
    conviction: int = 0,
    iv_rank: float = 0,
    dte: int = 0,
) -> dict:
    """Execute an entry trade with idempotency checking and fill confirmation.

    1. Check for duplicate intents
    2. Record order intent (PENDING)
    3. Submit limit order to broker
    4. Poll for fill status
    5. Create position record with actual fill price/qty
    6. Send Telegram notification

    Returns result dict with success/failure details.
    """
    # Position sizing — cap quantity based on equity and limits
    sizing = calculate_position_size(limit_price)
    if sizing.get("max_contracts", 0) <= 0:
        error_detail = sizing.get("error", sizing.get("limiting_factor", "no capacity"))
        log.warning("position_size_zero", ticker=ticker, detail=error_detail, sizing=sizing)
        return {"success": False, "error": f"Position sizing: {error_detail}"}

    if quantity > sizing["max_contracts"]:
        log.info(
            "position_size_capped",
            ticker=ticker,
            requested=quantity,
            capped=sizing["max_contracts"],
            reason=sizing["limiting_factor"],
        )
        quantity = sizing["max_contracts"]

    # Hard safety gate — deterministic, non-overridable
    gate = get_safety_gate()
    allowed, reason = gate.check_entry({
        "signal_id": signal_id,
        "ticker": ticker,
        "option_symbol": option_symbol,
        "quantity": quantity,
        "limit_price": limit_price,
        "iv_rank": iv_rank,
        "dte": dte,
    })
    if not allowed:
        return {"success": False, "error": f"Safety gate blocked: {reason}"}

    session = get_session()
    try:
        # Idempotency check
        idemp_key = f"entry-{signal_id}"
        existing = session.query(OrderIntent).filter(OrderIntent.idempotency_key == idemp_key).first()
        if existing:
            log.warning("duplicate_entry", signal_id=signal_id, status=existing.status.value)
            return {
                "success": False,
                "error": f"Duplicate order intent for signal {signal_id} (status: {existing.status.value})",
            }

        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        # Record intent
        intent = OrderIntent(
            idempotency_key=idemp_key,
            signal_id=signal_id,
            ticker=ticker,
            option_symbol=option_symbol,
            side=order_side,
            quantity=quantity,
            limit_price=limit_price,
            status=IntentStatus.PENDING,
            reason=thesis,
        )
        session.add(intent)
        session.commit()

        # Submit to broker
        broker = AlpacaBroker()
        result = broker.submit_limit_order(
            symbol=option_symbol,
            side=order_side,
            qty=quantity,
            limit_price=limit_price,
        )

        if not result.success:
            intent.status = IntentStatus.FAILED
            session.commit()
            log.error("entry_failed", ticker=ticker, error=result.error)
            notifier = TelegramNotifier()
            await notifier.notify_error("Entry execution", result.error)
            return {"success": False, "error": result.error}

        # Record broker order as SUBMITTED
        intent.broker_order_id = result.broker_order_id
        broker_order = BrokerOrder(
            broker_order_id=result.broker_order_id,
            intent_id=idemp_key,
            ticker=ticker,
            option_symbol=option_symbol,
            side=order_side,
            quantity=quantity,
            order_type="limit",
            limit_price=limit_price,
            status=OrderStatus.SUBMITTED,
        )
        session.add(broker_order)
        session.commit()

        # Poll for fill
        fill_status = await _wait_for_fill(broker, result.broker_order_id)
        filled_qty = fill_status.get("filled_qty", 0)
        filled_price = fill_status.get("filled_avg_price")
        order_state = fill_status.get("status", "").lower()

        # Update broker order record
        if order_state == "filled":
            broker_order.status = OrderStatus.FILLED
            broker_order.filled_qty = filled_qty
            broker_order.filled_price = filled_price
            broker_order.filled_at = datetime.now(timezone.utc)
            intent.status = IntentStatus.EXECUTED
            intent.executed_at = datetime.now(timezone.utc)
        elif filled_qty > 0:
            broker_order.status = OrderStatus.PARTIAL
            broker_order.filled_qty = filled_qty
            broker_order.filled_price = filled_price
            intent.status = IntentStatus.EXECUTED
            intent.executed_at = datetime.now(timezone.utc)
        else:
            # No fill yet — order is still working
            broker_order.status = OrderStatus.SUBMITTED
            # Keep intent as PENDING so reconcile_orders can update later

        # Parse OCC symbol for metadata
        parsed = parse_occ_symbol(option_symbol)
        pos_action = SignalAction.CALL
        pos_strike = 0.0
        pos_expiration = ""
        if parsed:
            pos_action = SignalAction.CALL if parsed.option_type == "CALL" else SignalAction.PUT
            pos_strike = parsed.strike
            pos_expiration = parsed.expiration

        # Use actual fill price if available, otherwise limit price
        actual_price = filled_price if filled_price else limit_price
        actual_qty = filled_qty if filled_qty > 0 else quantity

        # Create position record
        position_id = uuid4().hex[:16]
        position = PositionRecord(
            position_id=position_id,
            signal_id=signal_id,
            ticker=ticker,
            option_symbol=option_symbol,
            action=pos_action,
            strike=pos_strike,
            expiration=pos_expiration,
            quantity=actual_qty,
            entry_price=actual_price,
            entry_value=actual_price * actual_qty * 100,
            status=PositionStatus.OPEN,
            entry_thesis=thesis,
            conviction=conviction,
        )
        session.add(position)
        session.commit()

        # Notify
        notifier = TelegramNotifier()
        fill_note = ""
        if filled_qty > 0 and filled_qty < quantity:
            fill_note = f" (PARTIAL: {filled_qty}/{quantity} filled)"
        elif filled_qty == 0:
            fill_note = " (PENDING FILL — order working)"

        await notifier.notify_entry(
            ticker=ticker,
            action=order_side.value,
            strike=pos_strike,
            expiration=pos_expiration,
            quantity=actual_qty,
            price=actual_price,
            thesis=thesis + fill_note,
            conviction=conviction,
        )

        log.info(
            "entry_executed",
            ticker=ticker,
            order_id=result.broker_order_id,
            position_id=position_id,
            filled_qty=filled_qty,
            filled_price=filled_price,
            order_state=order_state,
        )
        return {
            "success": True,
            "broker_order_id": result.broker_order_id,
            "position_id": position_id,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
            "order_status": order_state,
            "message": f"Order {order_state}: {filled_qty}/{quantity} filled @ ${actual_price:.2f}{fill_note}",
        }
    except Exception as e:
        session.rollback()
        log.error("entry_exception", ticker=ticker, error=str(e))
        return {"success": False, "error": str(e)}
    finally:
        session.close()


async def execute_exit(
    position_id: str,
    reason: str = "",
    use_market: bool = False,
) -> dict:
    """Execute an exit trade for an open position with fill confirmation.

    1. Look up position
    2. Submit sell order
    3. Poll for fill
    4. Update position status and P&L with actual fill price
    5. Record trade log
    6. Send notification

    Returns result dict.
    """
    session = get_session()
    try:
        pos = session.query(PositionRecord).filter(PositionRecord.position_id == position_id).first()
        if not pos:
            return {"success": False, "error": f"Position {position_id} not found"}
        if pos.status != PositionStatus.OPEN:
            return {"success": False, "error": f"Position {position_id} is {pos.status.value}"}

        # Idempotency
        idemp_key = f"exit-{position_id}"
        existing = session.query(OrderIntent).filter(OrderIntent.idempotency_key == idemp_key).first()
        if existing and existing.status == IntentStatus.EXECUTED:
            return {"success": False, "error": f"Exit already executed for {position_id}"}

        # Record intent
        intent = OrderIntent(
            idempotency_key=idemp_key,
            signal_id=pos.signal_id,
            ticker=pos.ticker,
            option_symbol=pos.option_symbol,
            side=OrderSide.SELL,
            quantity=pos.quantity,
            status=IntentStatus.PENDING,
            reason=reason,
        )
        session.add(intent)
        session.commit()

        broker = AlpacaBroker()
        if use_market:
            result = broker.submit_market_order(
                symbol=pos.option_symbol,
                side=OrderSide.SELL,
                qty=pos.quantity,
            )
        else:
            exit_price = pos.current_price or pos.entry_price
            settings = get_settings()
            buffer = 1 - (settings.trading.limit_price_buffer_pct / 100)
            limit_price = round(exit_price * buffer, 2)
            result = broker.submit_limit_order(
                symbol=pos.option_symbol,
                side=OrderSide.SELL,
                qty=pos.quantity,
                limit_price=limit_price,
            )

        if not result.success:
            intent.status = IntentStatus.FAILED
            session.commit()
            log.error("exit_failed", position_id=position_id, error=result.error)
            return {"success": False, "error": result.error}

        intent.broker_order_id = result.broker_order_id

        # Poll for fill
        fill_status = await _wait_for_fill(broker, result.broker_order_id)
        filled_qty = fill_status.get("filled_qty", 0)
        filled_price = fill_status.get("filled_avg_price")
        order_state = fill_status.get("status", "").lower()

        if order_state == "filled" or filled_qty > 0:
            intent.status = IntentStatus.EXECUTED
            intent.executed_at = datetime.now(timezone.utc)

            # Use actual fill price for P&L
            actual_exit_price = filled_price if filled_price else (pos.current_price or pos.entry_price)

            # Update position
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.now(timezone.utc)

            # Trade log with actual fill data
            pnl_dollars = (actual_exit_price - pos.entry_price) * pos.quantity * 100
            pnl_pct = ((actual_exit_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else 0
            hold_hours = 0.0
            if pos.opened_at:
                hold_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600

            trade = TradeLog(
                position_id=position_id,
                ticker=pos.ticker,
                action=pos.action,
                entry_price=pos.entry_price,
                exit_price=actual_exit_price,
                quantity=filled_qty if filled_qty > 0 else pos.quantity,
                pnl_dollars=round(pnl_dollars, 2),
                pnl_pct=round(pnl_pct, 2),
                hold_duration_hours=round(hold_hours, 1),
                entry_thesis=pos.entry_thesis,
                exit_reason=reason,
                opened_at=pos.opened_at,
            )
            session.add(trade)
            session.commit()

            # Notify
            notifier = TelegramNotifier()
            await notifier.notify_exit(
                ticker=pos.ticker,
                action=pos.action.value if pos.action else "SELL",
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                exit_price=actual_exit_price,
                pnl_pct=pnl_pct,
                pnl_dollars=pnl_dollars,
                reason=reason,
            )

            log.info("exit_executed", position_id=position_id, ticker=pos.ticker, pnl=f"${pnl_dollars:.2f}", fill_price=actual_exit_price)
            return {
                "success": True,
                "broker_order_id": result.broker_order_id,
                "filled_qty": filled_qty,
                "filled_price": actual_exit_price,
                "pnl_dollars": round(pnl_dollars, 2),
                "pnl_pct": round(pnl_pct, 2),
                "message": f"Exited {pos.ticker} — P&L: ${pnl_dollars:.2f} ({pnl_pct:.1f}%)",
            }
        else:
            # No fill yet — order still working
            session.commit()
            log.warning("exit_pending", position_id=position_id, order_state=order_state)
            return {
                "success": True,
                "broker_order_id": result.broker_order_id,
                "filled_qty": 0,
                "order_status": order_state,
                "message": f"Exit order submitted but not yet filled (status: {order_state})",
            }
    except Exception as e:
        session.rollback()
        log.error("exit_exception", position_id=position_id, error=str(e))
        return {"success": False, "error": str(e)}
    finally:
        session.close()


async def reconcile_orders() -> dict:
    """Reconcile all pending/submitted broker orders with actual fill status.

    Called periodically by the monitor loop to catch fills that happened
    after the initial poll timeout.

    Returns summary of reconciled orders.
    """
    session = get_session()
    try:
        pending_orders = (
            session.query(BrokerOrder)
            .filter(BrokerOrder.status.in_([OrderStatus.SUBMITTED, OrderStatus.PENDING]))
            .all()
        )

        if not pending_orders:
            return {"reconciled": 0, "message": "No pending orders"}

        broker = AlpacaBroker()
        reconciled = 0

        for order in pending_orders:
            status = broker.get_order_status(order.broker_order_id)
            order_state = status.get("status", "").lower()
            filled_qty = status.get("filled_qty", 0)
            filled_price = status.get("filled_avg_price")

            if order_state == "filled":
                order.status = OrderStatus.FILLED
                order.filled_qty = filled_qty
                order.filled_price = filled_price
                order.filled_at = datetime.now(timezone.utc)

                # Update the corresponding position's entry price if it was created with limit price
                _update_position_fill(session, order.intent_id, filled_price, filled_qty)
                reconciled += 1
                log.info("order_reconciled", order_id=order.broker_order_id, filled_price=filled_price)

            elif order_state in ("cancelled", "canceled", "expired", "rejected"):
                order.status = OrderStatus.CANCELLED
                order.error_msg = f"Order {order_state}"
                reconciled += 1
                log.info("order_cancelled_reconciled", order_id=order.broker_order_id, state=order_state)

            elif filled_qty > 0:
                order.status = OrderStatus.PARTIAL
                order.filled_qty = filled_qty
                order.filled_price = filled_price
                reconciled += 1

        session.commit()
        log.info("reconcile_complete", pending=len(pending_orders), reconciled=reconciled)
        return {"reconciled": reconciled, "pending_checked": len(pending_orders)}
    except Exception as e:
        session.rollback()
        log.error("reconcile_error", error=str(e))
        return {"reconciled": 0, "error": str(e)}
    finally:
        session.close()


def _update_position_fill(session: object, intent_id: str, filled_price: float | None, filled_qty: int) -> None:
    """Update position record with actual fill data."""
    if not filled_price:
        return
    intent = session.query(OrderIntent).filter(OrderIntent.idempotency_key == intent_id).first()
    if not intent:
        return
    position = (
        session.query(PositionRecord)
        .filter(PositionRecord.signal_id == intent.signal_id, PositionRecord.status == PositionStatus.OPEN)
        .first()
    )
    if position:
        position.entry_price = filled_price
        position.entry_value = filled_price * position.quantity * 100
        if filled_qty > 0:
            position.quantity = filled_qty
            position.entry_value = filled_price * filled_qty * 100


def get_account_info() -> dict:
    """Get broker account information."""
    broker = AlpacaBroker()
    return broker.get_account()


# ---------------------------------------------------------------------------
# Tool definitions for Claude agent SDK
# ---------------------------------------------------------------------------

EXECUTION_TOOLS = [
    {
        "name": "execute_entry",
        "description": (
            "Execute an approved entry trade. Checks idempotency, submits a limit order, "
            "polls for fill confirmation, creates a position record with actual fill price, "
            "and sends a Telegram notification. "
            "Only call this when the orchestrator has explicitly approved the trade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_id": {"type": "string", "description": "The signal ID triggering this entry"},
                "ticker": {"type": "string", "description": "The stock ticker"},
                "option_symbol": {"type": "string", "description": "Full OCC option symbol"},
                "side": {"type": "string", "enum": ["BUY", "SELL"], "description": "Order side"},
                "quantity": {"type": "integer", "description": "Number of contracts"},
                "limit_price": {"type": "number", "description": "Limit price per contract"},
                "thesis": {"type": "string", "description": "Entry thesis/reasoning"},
                "conviction": {"type": "integer", "description": "Conviction score 0-100"},
                "iv_rank": {"type": "number", "description": "IV rank percentage (0-100) from the signal"},
                "dte": {"type": "integer", "description": "Days to expiration from the signal"},
            },
            "required": ["signal_id", "ticker", "option_symbol", "side", "quantity", "limit_price"],
        },
    },
    {
        "name": "execute_exit",
        "description": (
            "Execute an exit for an open position. Submits a sell order, polls for fill, "
            "updates position status, records P&L with actual fill price, and sends notification."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position_id": {"type": "string", "description": "The position ID to exit"},
                "reason": {"type": "string", "description": "Exit reason (e.g., profit_target, stop_loss, thesis_invalidation)"},
                "use_market": {"type": "boolean", "description": "Use market order instead of limit (for emergencies only)"},
            },
            "required": ["position_id"],
        },
    },
    {
        "name": "get_account_info",
        "description": "Get broker account details: equity, buying power, cash, portfolio value.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "calculate_position_size",
        "description": (
            "Calculate the maximum number of contracts for a new position based on "
            "current equity, existing exposure, and risk limits. Call this BEFORE "
            "execute_entry to determine appropriate quantity. Returns max_contracts "
            "along with the limiting factor (per_trade_pct, position_value_cap, or total_exposure)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "option_price": {
                    "type": "number",
                    "description": "The option premium per contract (e.g., 2.50 for $250/contract)",
                },
            },
            "required": ["option_price"],
        },
    },
]
