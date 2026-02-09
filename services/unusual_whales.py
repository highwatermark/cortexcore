"""
Unusual Whales API client for options flow data.

Uses the /screener/option-contracts endpoint which provides per-contract
flow data including volume, OI, sweep/floor volume, premium, Greeks,
and stock price. Includes retry with exponential backoff (2 retries, 2s base).
"""
from __future__ import annotations

import asyncio

import httpx

from config.settings import get_settings
from core.logger import get_logger
from core.utils import calc_dte, parse_occ_symbol
from data.models import FlowSignal, SignalAction

log = get_logger("uw_client")

BASE_URL = "https://api.unusualwhales.com/api"
MAX_RETRIES = 2
RETRY_BASE_DELAY = 2  # seconds, exponential


class UnusualWhalesClient:
    """Async client for the Unusual Whales options flow API."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.api.uw_api_key
        self._flow_cfg = settings.flow
        self._excluded = settings.excluded_tickers
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    async def fetch_flow(self) -> list[FlowSignal]:
        """Fetch high-activity option contracts from the screener endpoint.

        Uses /screener/option-contracts with filters for premium, volume,
        and issue type. Returns parsed and filtered FlowSignal objects.
        """
        params: dict = {
            "limit": self._flow_cfg.scan_limit,
            "order_by": "volume",
            "order": "desc",
            "min_premium": self._flow_cfg.min_premium,
        }

        # Filter to common stocks only
        if self._flow_cfg.issue_types:
            params["issue_types"] = ",".join(self._flow_cfg.issue_types)

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"{BASE_URL}/screener/option-contracts",
                        headers=self._headers,
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    log.warning("uw_api_retry", attempt=attempt + 1, delay=delay, error=str(e))
                    await asyncio.sleep(delay)
                else:
                    log.error("uw_api_failed", attempts=MAX_RETRIES + 1, error=str(e))
                    raise

        flows = data.get("data", [])
        log.info("uw_flow_fetched", count=len(flows))

        signals: list[FlowSignal] = []
        for item in flows:
            signal = self._parse_screener_item(item)
            if signal is not None:
                signals.append(signal)

        log.info("uw_signals_parsed", total=len(flows), passed_filter=len(signals))
        return signals

    async def get_option_contracts(self, ticker: str) -> list[dict]:
        """Get option contracts for a specific ticker."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{BASE_URL}/stock/{ticker}/option-contracts",
                headers=self._headers,
                params={"limit": 50},
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    async def get_next_earnings_date(self, ticker: str) -> str | None:
        """Get the next earnings date for a ticker.

        Returns ISO date string (YYYY-MM-DD) or None if unavailable.
        Uses /api/stock/{ticker}/company endpoint which includes earnings data.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{BASE_URL}/stock/{ticker}/company",
                    headers=self._headers,
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                # Try common field names for next earnings
                for field in ("next_earnings_date", "earnings_date", "next_earnings"):
                    val = data.get(field)
                    if val:
                        return str(val)[:10]  # YYYY-MM-DD
                return None
        except Exception as e:
            log.warning("earnings_fetch_failed", ticker=ticker, error=str(e))
            return None

    def _parse_screener_item(self, item: dict) -> FlowSignal | None:
        """Parse a screener result into a FlowSignal, or None if filtered."""
        ticker = (item.get("ticker_symbol", "") or "").upper().strip()

        # Filter excluded tickers
        if ticker in self._excluded or not ticker:
            return None

        # Filter issue type
        issue_type = item.get("issue_type", "")
        if issue_type and issue_type not in self._flow_cfg.issue_types:
            return None

        # Parse OCC symbol for strike/expiration/type
        occ_sym = item.get("option_symbol", "")
        parsed = parse_occ_symbol(occ_sym)
        if not parsed:
            return None

        option_type = parsed.option_type
        strike = parsed.strike
        expiration = parsed.expiration

        # Parse premium
        try:
            premium = float(item.get("premium", 0) or 0)
        except (ValueError, TypeError):
            return None
        if premium < self._flow_cfg.min_premium:
            return None

        # Parse volume/OI
        volume = int(item.get("volume", 0) or 0)
        oi = int(item.get("open_interest", 0) or 0)
        if oi < self._flow_cfg.min_open_interest:
            return None

        vol_oi = volume / oi if oi > 0 else 0.0
        if vol_oi < self._flow_cfg.min_vol_oi_ratio:
            return None

        # DTE check
        dte = calc_dte(expiration)
        if dte < self._flow_cfg.min_dte or dte > self._flow_cfg.max_dte:
            return None

        # Underlying price and strike distance
        underlying_price = float(item.get("stock_price", 0) or 0)
        if underlying_price > 0 and strike > 0:
            distance = abs(strike - underlying_price) / underlying_price
            if distance > self._flow_cfg.max_strike_distance_pct:
                return None

        # Determine order type from volume breakdown
        sweep_vol = int(item.get("sweep_volume", 0) or 0)
        floor_vol = int(item.get("floor_volume", 0) or 0)
        order_parts: list[str] = []
        if sweep_vol > 0 and sweep_vol / volume > 0.1:
            order_parts.append("sweep")
        if floor_vol > 0 and floor_vol / volume > 0.05:
            order_parts.append("floor")
        # Check if this is a new/opening position (volume > previous OI)
        prev_oi = int(item.get("prev_oi", 0) or 0)
        if prev_oi > 0 and volume > prev_oi:
            order_parts.append("open")
        order_type = " ".join(order_parts) if order_parts else "regular"

        return FlowSignal(
            ticker=ticker,
            action=SignalAction.CALL if option_type == "CALL" else SignalAction.PUT,
            strike=strike,
            expiration=expiration,
            premium=premium,
            volume=volume,
            open_interest=oi,
            vol_oi_ratio=round(vol_oi, 2),
            option_type=option_type,
            order_type=order_type,
            underlying_price=underlying_price,
            iv_rank=0.0,  # Not directly available from screener; IV is per-contract
            dte=dte,
        )
