"""Tests for true IV rank computation from historical volatility."""
from __future__ import annotations

import math
from datetime import date
from unittest.mock import patch, MagicMock

import pytest


class TestComputeIvRank:
    def test_iv_rank_low_vs_history(self) -> None:
        """Very low current IV should produce low rank."""
        from services.alpaca_options_data import AlpacaOptionsData

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._stock_client = None
            client._settings = MagicMock()
            client._settings.api.alpaca_api_key = "test"
            client._settings.api.alpaca_secret_key = "test"
            client._iv_rank_cache = {}

            # Build mock bars: 253 days, steady ~0.1% daily return
            mock_bars = []
            for i in range(253):
                bar = MagicMock()
                bar.close = 100.0 * (1.001 ** i)
                mock_bars.append(bar)

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars', return_value=mock_bars):
                # Realized vol from steady 0.1% daily return is ~0.016 annualized.
                # current_iv=0.005 is well below, so rank should be low.
                rank = client.compute_iv_rank("AAPL", current_iv=0.005)
                assert rank < 30

    def test_iv_rank_high_vs_history(self) -> None:
        """Very high current IV should produce high rank."""
        from services.alpaca_options_data import AlpacaOptionsData

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._stock_client = None
            client._settings = MagicMock()
            client._settings.api.alpaca_api_key = "test"
            client._settings.api.alpaca_secret_key = "test"
            client._iv_rank_cache = {}

            mock_bars = []
            for i in range(253):
                bar = MagicMock()
                bar.close = 100.0 * (1.001 ** i)
                mock_bars.append(bar)

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars', return_value=mock_bars):
                rank = client.compute_iv_rank("AAPL", current_iv=0.50)
                assert rank > 80

    def test_iv_rank_returns_zero_on_no_history(self) -> None:
        """If no historical data, return 0 (fail-open)."""
        from services.alpaca_options_data import AlpacaOptionsData

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._iv_rank_cache = {}

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars', return_value=[]):
                rank = client.compute_iv_rank("AAPL", current_iv=0.35)
                assert rank == 0.0

    def test_iv_rank_cache_hit(self) -> None:
        """Second call for same ticker should use cache, not re-fetch."""
        from services.alpaca_options_data import AlpacaOptionsData

        with patch.object(AlpacaOptionsData, '__init__', lambda self: None):
            client = AlpacaOptionsData()
            client._iv_rank_cache = {
                "AAPL": {
                    "date": date.today().isoformat(),
                    "vol_distribution": [0.15, 0.20, 0.25, 0.30, 0.35],
                }
            }

            with patch.object(AlpacaOptionsData, '_fetch_historical_bars') as mock_fetch:
                rank = client.compute_iv_rank("AAPL", current_iv=0.22)
                mock_fetch.assert_not_called()
                assert 20 < rank < 60
