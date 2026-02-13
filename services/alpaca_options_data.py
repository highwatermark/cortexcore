"""
Alpaca options data client for fetching Greeks, IV, and prices.

Wraps alpaca-py OptionHistoricalDataClient for snapshot queries.
"""
from __future__ import annotations

import yfinance as yf
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockSnapshotRequest

from config.settings import get_settings
from core.logger import get_logger

log = get_logger("alpaca_options_data")

_data_instance: AlpacaOptionsData | None = None


def get_options_data_client() -> AlpacaOptionsData:
    """Get or create the singleton AlpacaOptionsData instance."""
    global _data_instance
    if _data_instance is None:
        _data_instance = AlpacaOptionsData()
    return _data_instance


class AlpacaOptionsData:
    """Synchronous Alpaca options data client for snapshots."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = OptionHistoricalDataClient(
            api_key=self._settings.api.alpaca_api_key,
            secret_key=self._settings.api.alpaca_secret_key,
        )
        self._stock_client: StockHistoricalDataClient | None = None

    def _get_stock_client(self) -> StockHistoricalDataClient:
        if self._stock_client is None:
            self._stock_client = StockHistoricalDataClient(
                api_key=self._settings.api.alpaca_api_key,
                secret_key=self._settings.api.alpaca_secret_key,
            )
        return self._stock_client

    def get_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch option snapshots for a list of OCC symbols.

        Returns {symbol: {current_price, delta, gamma, theta, vega, iv}}
        for each symbol that has data. Missing/illiquid symbols are omitted.
        """
        if not symbols:
            return {}

        try:
            request = OptionSnapshotRequest(symbol_or_symbols=symbols)
            snapshots = self._client.get_option_snapshot(request)
        except Exception as e:
            log.error("snapshot_fetch_failed", error=str(e), symbol_count=len(symbols))
            return {}

        results: dict[str, dict] = {}
        for symbol, snap in snapshots.items():
            try:
                # Price: prefer mid-quote, fall back to latest trade
                current_price = None
                if snap.latest_quote:
                    bid = snap.latest_quote.bid_price
                    ask = snap.latest_quote.ask_price
                    if bid and ask and bid > 0 and ask > 0:
                        current_price = round((bid + ask) / 2, 4)
                if current_price is None and snap.latest_trade:
                    current_price = snap.latest_trade.price

                if current_price is None:
                    log.debug("no_price_data", symbol=symbol)
                    continue

                greeks = snap.greeks
                results[symbol] = {
                    "current_price": current_price,
                    "delta": greeks.delta if greeks else None,
                    "gamma": greeks.gamma if greeks else None,
                    "theta": greeks.theta if greeks else None,
                    "vega": greeks.vega if greeks else None,
                    "iv": snap.implied_volatility,
                }
            except Exception as e:
                log.warning("snapshot_parse_error", symbol=symbol, error=str(e))

        log.info("snapshots_fetched", requested=len(symbols), returned=len(results))
        return results

    def get_market_context(self) -> dict:
        """Fetch VIX index and SPY market context for Claude decisions.

        VIX is fetched via yfinance (^VIX) since Alpaca doesn't serve
        index data.  SPY is still fetched from Alpaca.

        Returns {
            "vix_level": float,       # actual VIX index level
            "vix_change_pct": float,   # daily change %
            "spy_price": float,
            "spy_change_pct": float,   # daily change %
            "regime": str,             # LOW_VOL / NORMAL / ELEVATED / HIGH_VOL
        }
        """
        result: dict = {}

        # --- VIX via yfinance (actual CBOE VIX index) ---
        try:
            vix_ticker = yf.Ticker("^VIX")
            fi = vix_ticker.fast_info
            vix_price = fi.get("lastPrice") or fi.get("last_price")
            vix_prev = fi.get("previousClose") or fi.get("previous_close")
            if vix_price and vix_price > 0:
                result["vix_level"] = round(vix_price, 2)
                if vix_prev and vix_prev > 0:
                    result["vix_change_pct"] = round((vix_price - vix_prev) / vix_prev * 100, 2)
                else:
                    result["vix_change_pct"] = 0.0
        except Exception as e:
            log.error("vix_fetch_failed", error=str(e))

        # --- SPY via Alpaca ---
        try:
            client = self._get_stock_client()
            request = StockSnapshotRequest(symbol_or_symbols=["SPY"])
            snapshots = client.get_stock_snapshot(request)
            snap = snapshots.get("SPY")
            if snap:
                price = snap.daily_bar.close if snap.daily_bar else None
                prev_close = snap.previous_daily_bar.close if snap.previous_daily_bar else None
                if price:
                    result["spy_price"] = price
                    if prev_close and prev_close > 0:
                        result["spy_change_pct"] = round((price - prev_close) / prev_close * 100, 2)
                    else:
                        result["spy_change_pct"] = 0.0
        except Exception as e:
            log.error("spy_fetch_failed", error=str(e))

        if "vix_level" in result:
            result["regime"] = self._classify_regime(result["vix_level"])

        log.info("market_context_fetched", **{k: v for k, v in result.items() if v is not None})
        return result

    @staticmethod
    def _classify_regime(vix_level: float) -> str:
        """Classify volatility regime based on actual VIX index level."""
        if vix_level < 15:
            return "LOW_VOL"
        elif vix_level < 20:
            return "NORMAL"
        elif vix_level < 30:
            return "ELEVATED"
        else:
            return "HIGH_VOL"
