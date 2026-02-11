"""
Alpaca options data client for fetching Greeks, IV, and prices.

Wraps alpaca-py OptionHistoricalDataClient for snapshot queries.
"""
from __future__ import annotations

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest

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
        settings = get_settings()
        self._client = OptionHistoricalDataClient(
            api_key=settings.api.alpaca_api_key,
            secret_key=settings.api.alpaca_secret_key,
        )

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
