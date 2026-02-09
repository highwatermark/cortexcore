"""
Telegram notification service.

Sends trade alerts, errors, and daily summaries to the configured admin chat.
Includes retry with linear backoff (3 retries, 500ms base).
"""
from __future__ import annotations

import asyncio

import httpx

from config.settings import get_settings
from core.logger import get_logger

log = get_logger("telegram")

TELEGRAM_API = "https://api.telegram.org"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5  # 500ms linear backoff


class TelegramNotifier:
    """Async Telegram bot for trade notifications."""

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.api.telegram_bot_token
        self._chat_id = settings.api.telegram_chat_id
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            log.warning("telegram_disabled", reason="missing token or chat_id")

    async def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the admin chat with retry."""
        if not self._enabled:
            log.debug("telegram_skip", reason="disabled")
            return False

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                        json={
                            "chat_id": self._chat_id,
                            "text": message,
                            "parse_mode": parse_mode,
                        },
                    )
                    resp.raise_for_status()
                    log.debug("telegram_sent", length=len(message))
                    return True
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (attempt + 1)
                    log.warning("telegram_retry", attempt=attempt + 1, delay=delay, error=str(e))
                    await asyncio.sleep(delay)

        log.error("telegram_error", error=str(last_error), attempts=MAX_RETRIES)
        return False

    async def notify_entry(
        self,
        ticker: str,
        action: str,
        strike: float,
        expiration: str,
        quantity: int,
        price: float,
        thesis: str,
        conviction: int,
    ) -> bool:
        """Send entry notification."""
        msg = (
            f"<b>NEW ENTRY</b>\n"
            f"<b>{ticker}</b> {action} ${strike} exp {expiration}\n"
            f"Qty: {quantity} @ ${price:.2f}\n"
            f"Conviction: {conviction}%\n"
            f"Thesis: {thesis}"
        )
        return await self.send(msg)

    async def notify_exit(
        self,
        ticker: str,
        action: str,
        quantity: int,
        entry_price: float,
        exit_price: float,
        pnl_pct: float,
        pnl_dollars: float,
        reason: str,
    ) -> bool:
        """Send exit notification."""
        emoji = "+" if pnl_dollars >= 0 else ""
        msg = (
            f"<b>EXIT</b>\n"
            f"<b>{ticker}</b> {action}\n"
            f"Qty: {quantity}\n"
            f"Entry: ${entry_price:.2f} -> Exit: ${exit_price:.2f}\n"
            f"P&L: {emoji}${pnl_dollars:.2f} ({emoji}{pnl_pct:.1f}%)\n"
            f"Reason: {reason}"
        )
        return await self.send(msg)

    async def notify_error(self, context: str, error: str) -> bool:
        """Send error notification."""
        msg = f"<b>ERROR</b>\nContext: {context}\nError: {error}"
        return await self.send(msg)

    async def notify_daily_summary(
        self,
        total_pnl: float,
        trades_today: int,
        open_positions: int,
        risk_score: int,
    ) -> bool:
        """Send end-of-day summary."""
        emoji = "+" if total_pnl >= 0 else ""
        msg = (
            f"<b>DAILY SUMMARY</b>\n"
            f"P&L: {emoji}${total_pnl:.2f}\n"
            f"Trades: {trades_today}\n"
            f"Open Positions: {open_positions}\n"
            f"Risk Score: {risk_score}/100"
        )
        return await self.send(msg)
