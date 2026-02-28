"""
Alpaca broker client for options trading.

Wraps alpaca-py SDK for order submission, position queries, and account info.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from config.settings import get_settings
from core.logger import get_logger
from core.utils import calc_dte, parse_occ_symbol
from data.models import OrderSide, PositionSnapshot, SignalAction, TradeResult

log = get_logger("alpaca_broker")

_broker_instance: AlpacaBroker | None = None


def get_broker() -> AlpacaBroker:
    """Get or create the singleton AlpacaBroker instance."""
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = AlpacaBroker()
    return _broker_instance


class AlpacaBroker:
    """Synchronous Alpaca trading client for options."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = TradingClient(
            api_key=settings.api.alpaca_api_key,
            secret_key=settings.api.alpaca_secret_key,
            paper=settings.paper_trading,
            url_override=settings.api.alpaca_base_url,
        )
        self._trading = settings.trading
        self._shadow = settings.shadow_mode

    def get_account(self) -> dict:
        """Get account info (equity, buying power, etc.)."""
        acct = self._client.get_account()
        return {
            "equity": float(acct.equity),
            "buying_power": float(acct.buying_power),
            "cash": float(acct.cash),
            "portfolio_value": float(acct.portfolio_value),
            "currency": acct.currency,
        }

    def get_positions(self) -> list[PositionSnapshot]:
        """Get all open positions as PositionSnapshot objects."""
        positions = self._client.get_all_positions()
        snapshots: list[PositionSnapshot] = []

        for pos in positions:
            if not hasattr(pos, "symbol") or not pos.symbol:
                continue

            try:
                # Only include options positions (must parse as OCC symbol)
                parsed = parse_occ_symbol(pos.symbol)
                if not parsed:
                    log.debug("skipping_non_option", symbol=pos.symbol)
                    continue

                entry_price = float(pos.avg_entry_price)
                current_price = float(pos.current_price) if pos.current_price else entry_price
                qty = int(pos.qty)
                pnl_dollars = float(pos.unrealized_pl) if pos.unrealized_pl else 0.0
                pnl_pct = float(pos.unrealized_plpc) if pos.unrealized_plpc else 0.0

                ticker = parsed.ticker
                action = SignalAction.CALL if parsed.option_type == "CALL" else SignalAction.PUT
                strike = parsed.strike
                expiration = parsed.expiration
                dte_remaining = calc_dte(expiration)

                snapshots.append(PositionSnapshot(
                    position_id=str(pos.asset_id) if pos.asset_id else pos.symbol,
                    ticker=ticker,
                    option_symbol=pos.symbol,
                    action=action,
                    strike=strike,
                    expiration=expiration,
                    quantity=qty,
                    entry_price=entry_price,
                    current_price=current_price,
                    pnl_pct=round(pnl_pct * 100, 2),
                    pnl_dollars=round(pnl_dollars, 2),
                    dte_remaining=dte_remaining,
                ))
            except (ValueError, AttributeError) as e:
                log.warning("position_parse_error", symbol=pos.symbol, error=str(e))

        return snapshots

    def submit_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        limit_price: float,
    ) -> TradeResult:
        """Submit a limit order for an options contract."""
        if self._shadow:
            log.info("shadow_order", symbol=symbol, side=side, qty=qty, price=limit_price)
            return TradeResult(
                success=True,
                broker_order_id="shadow-" + symbol,
                filled_price=limit_price,
                filled_qty=qty,
                message="Shadow mode — order not submitted",
            )

        try:
            alpaca_side = AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL
            order_req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=alpaca_side,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            order = self._client.submit_order(order_req)
            log.info("order_submitted", order_id=order.id, symbol=symbol, side=side.value)

            return TradeResult(
                success=True,
                broker_order_id=str(order.id),
                message=f"Order submitted: {order.id}",
            )
        except Exception as e:
            log.error("order_failed", symbol=symbol, error=str(e))
            return TradeResult(success=False, error=str(e))

    def submit_market_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
    ) -> TradeResult:
        """Submit a market order (used only for emergency stop-loss exits)."""
        if self._shadow:
            log.info("shadow_market_order", symbol=symbol, side=side, qty=qty)
            return TradeResult(
                success=True,
                broker_order_id="shadow-mkt-" + symbol,
                message="Shadow mode — market order not submitted",
            )

        try:
            alpaca_side = AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL
            order_req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=alpaca_side,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(order_req)
            log.info("market_order_submitted", order_id=order.id, symbol=symbol)

            return TradeResult(
                success=True,
                broker_order_id=str(order.id),
                message=f"Market order submitted: {order.id}",
            )
        except Exception as e:
            log.error("market_order_failed", symbol=symbol, error=str(e))
            return TradeResult(success=False, error=str(e))

    def get_order_status(self, order_id: str) -> dict:
        """Check the status of an existing order."""
        try:
            order = self._client.get_order_by_id(order_id)
            # Normalize status: Alpaca returns enum objects (e.g. OrderStatus.filled).
            # Extract the .value for consistent lowercase string comparison downstream.
            raw_status = order.status
            if hasattr(raw_status, "value"):
                status_str = str(raw_status.value).lower()
            else:
                status_str = str(raw_status).lower()
            return {
                "id": str(order.id),
                "status": status_str,
                "filled_qty": int(order.filled_qty or 0),
                "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
                "symbol": order.symbol,
            }
        except Exception as e:
            log.error("order_status_error", order_id=order_id, error=str(e))
            return {"id": order_id, "status": "UNKNOWN", "error": str(e)}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            self._client.cancel_order_by_id(order_id)
            log.info("order_cancelled", order_id=order_id)
            return True
        except Exception as e:
            log.error("cancel_failed", order_id=order_id, error=str(e))
            return False

    def is_market_open_today(self) -> bool:
        """Check if the market is open today using Alpaca's calendar API.

        Handles holidays and half days.
        """
        try:
            from alpaca.trading.requests import GetCalendarRequest
            from core.utils import trading_today
            today = trading_today()
            cal_request = GetCalendarRequest(start=today, end=today)
            calendars = self._client.get_calendar(cal_request)
            if not calendars:
                return False
            cal_day = calendars[0]
            return str(cal_day.date) == today
        except Exception as e:
            log.warning("calendar_check_failed", error=str(e))
            # Fallback: assume open on weekdays (ET)
            from core.utils import trading_now
            return trading_now().weekday() < 5

    @staticmethod
    def _extract_ticker(symbol: str) -> str:
        """Extract base ticker from an option symbol like AAPL250321C00175000."""
        for i, c in enumerate(symbol):
            if c.isdigit():
                return symbol[:i]
        return symbol
