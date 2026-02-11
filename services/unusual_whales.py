"""
Unusual Whales API client for options flow data.

Uses the /option-trades/flow-alerts endpoint which provides per-alert
flow data including volume, OI, sweep/floor flags, premium, and stock
price.

Dedup strategy (two layers):
  1. Server-side: ``newer_than`` set to the max ``created_at`` from the
     previous response so the API only returns alerts created since then.
  2. Client-side: ``_seen_ids`` set of alert UUIDs as a safety net to
     never process the same alert twice.

Includes retry with exponential backoff (2 retries, 2s base).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from config.settings import get_settings
from core.logger import get_logger
from core.utils import calc_dte, parse_occ_symbol
from data.models import FlowSignal, SignalAction

log = get_logger("uw_client")

BASE_URL = "https://api.unusualwhales.com/api"
MAX_RETRIES = 2
RETRY_BASE_DELAY = 2  # seconds, exponential

# Server-side cursor: unix seconds of the max created_at from the
# previous response.  newer_than filters on created_at (NOT start_time).
_newer_than_ts: int = 0

# Client-side safety net: set of alert IDs already processed.
_seen_ids: set[str] = set()


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
        """Fetch new flow alerts since the last poll.

        Two-layer dedup:
          1. ``newer_than`` (server-side) — set to the max ``created_at``
             from the previous response so only new alerts are returned.
          2. ``_seen_ids`` (client-side) — skip any alert ID already
             processed, as a safety net for edge cases.
        """
        global _newer_than_ts

        params: dict = {
            "limit": self._flow_cfg.scan_limit,
            "min_premium": self._flow_cfg.min_premium,
        }

        # Filter to common stocks only
        if self._flow_cfg.issue_types:
            params["issue_types[]"] = ",".join(self._flow_cfg.issue_types)

        # Server-side cursor: only fetch alerts created after our last poll.
        # newer_than filters on created_at (NOT start_time).
        if _newer_than_ts > 0:
            params["newer_than"] = str(_newer_than_ts)

        # Additional API-level filters
        if self._flow_cfg.min_dte > 0:
            params["min_dte"] = self._flow_cfg.min_dte
        if self._flow_cfg.max_dte > 0:
            params["max_dte"] = self._flow_cfg.max_dte
        if self._flow_cfg.all_opening:
            params["all_opening"] = "true"
        if self._flow_cfg.min_vol_oi_ratio > 0:
            params["min_volume_oi_ratio"] = str(self._flow_cfg.min_vol_oi_ratio)

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"{BASE_URL}/option-trades/flow-alerts",
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
        if isinstance(flows, dict):
            flows = [flows]

        # Advance the server-side cursor to the max created_at so the
        # next poll only gets alerts created after this point.
        if flows:
            max_created = 0
            for item in flows:
                created = item.get("created_at", "")
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        ts = int(dt.timestamp())
                        if ts > max_created:
                            max_created = ts
                    except (ValueError, TypeError):
                        pass
            if max_created > 0:
                _newer_than_ts = max_created + 1  # exclusive: skip the last seen second

        # Client-side dedup: skip any alert ID already processed
        new_flows = []
        for item in flows:
            alert_id = item.get("id", "")
            if alert_id and alert_id not in _seen_ids:
                new_flows.append(item)
                _seen_ids.add(alert_id)

        log.info(
            "uw_flow_fetched",
            count=len(flows),
            new=len(new_flows),
            seen_total=len(_seen_ids),
            newer_than=_newer_than_ts or "none",
        )

        signals: list[FlowSignal] = []
        for item in new_flows:
            if not isinstance(item, dict):
                continue
            signal = self._parse_flow_alert(item)
            if signal is not None:
                signals.append(signal)

        log.info("uw_signals_parsed", total=len(new_flows), passed_filter=len(signals))
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

    def _parse_flow_alert(self, item: dict) -> FlowSignal | None:
        """Parse a flow-alert into a FlowSignal, or None if filtered out.

        The /option-trades/flow-alerts response uses different field names
        than the screener endpoint (e.g. ``ticker`` not ``ticker_symbol``,
        boolean flags instead of volume breakdowns).
        """
        ticker = (item.get("ticker", "") or "").upper().strip()

        # Filter excluded tickers
        if ticker in self._excluded or not ticker:
            return None

        # Filter issue type
        issue_type = item.get("issue_type", "")
        if issue_type and issue_type not in self._flow_cfg.issue_types:
            return None

        # Parse OCC symbol for strike/expiration/type
        # flow-alerts uses "option_chain" for the OCC symbol
        occ_sym = item.get("option_chain", "") or item.get("option_symbol", "")
        parsed = parse_occ_symbol(occ_sym)
        if not parsed:
            return None

        option_type = parsed.option_type
        strike = parsed.strike
        expiration = parsed.expiration

        # Parse premium (flow-alerts returns string values)
        try:
            premium = float(item.get("total_premium", 0) or item.get("premium", 0) or 0)
        except (ValueError, TypeError):
            return None
        if premium < self._flow_cfg.min_premium:
            return None

        # Parse volume/OI
        volume = int(float(item.get("volume", 0) or 0))
        oi = int(float(item.get("open_interest", 0) or 0))
        if oi < self._flow_cfg.min_open_interest:
            return None

        vol_oi = float(item.get("volume_oi_ratio", 0) or 0)
        if vol_oi == 0 and oi > 0:
            vol_oi = volume / oi
        if vol_oi < self._flow_cfg.min_vol_oi_ratio:
            return None

        # DTE check
        dte = calc_dte(expiration)
        if dte < self._flow_cfg.min_dte or dte > self._flow_cfg.max_dte:
            return None

        # Underlying price and strike distance
        underlying_price = float(item.get("underlying_price", 0) or item.get("stock_price", 0) or 0)
        if underlying_price > 0 and strike > 0:
            distance = abs(strike - underlying_price) / underlying_price
            if distance > self._flow_cfg.max_strike_distance_pct:
                return None

        # Order type from boolean flags (flow-alerts format)
        order_parts: list[str] = []
        if item.get("has_sweep", False):
            order_parts.append("sweep")
        if item.get("has_floor", False):
            order_parts.append("floor")
        if item.get("all_opening_trades", False):
            order_parts.append("open")
        order_type = " ".join(order_parts) if order_parts else "regular"

        # Directional conviction from ask/bid side premium
        ask_prem = float(item.get("total_ask_side_prem", 0) or 0)
        bid_prem = float(item.get("total_bid_side_prem", 0) or 0)
        total_side_prem = ask_prem + bid_prem
        if total_side_prem > 0:
            directional_pct = max(ask_prem, bid_prem) / total_side_prem
        else:
            directional_pct = 0.0
        directional_side = "ASK" if ask_prem >= bid_prem else "BID"

        # Trade structure (directly available as booleans)
        has_singleleg = bool(item.get("has_singleleg", False))
        has_multileg = bool(item.get("has_multileg", False))
        trade_count = int(float(item.get("trade_count", 0) or 0))

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
            iv_rank=0.0,  # Not directly available from flow-alerts
            dte=dte,
            ask_side_volume=ask_prem,
            bid_side_volume=bid_prem,
            directional_pct=round(directional_pct, 4),
            directional_side=directional_side,
            has_singleleg=has_singleleg,
            has_multileg=has_multileg,
            trade_count=trade_count,
            next_earnings_date="",  # Fetched separately via get_next_earnings_date
        )
