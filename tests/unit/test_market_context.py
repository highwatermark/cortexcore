"""Tests for market context (VIX/SPY) fetching and regime classification."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.alpaca_options_data import AlpacaOptionsData


def _mock_snapshot(close: float, prev_close: float) -> MagicMock:
    """Build a mock stock snapshot with daily_bar and previous_daily_bar."""
    snap = MagicMock()
    snap.daily_bar.close = close
    snap.previous_daily_bar.close = prev_close
    return snap


class TestRegimeClassification:
    def test_low_vol(self) -> None:
        assert AlpacaOptionsData._classify_regime(12.0) == "LOW_VOL"

    def test_normal(self) -> None:
        assert AlpacaOptionsData._classify_regime(15.0) == "NORMAL"
        assert AlpacaOptionsData._classify_regime(19.9) == "NORMAL"

    def test_elevated(self) -> None:
        assert AlpacaOptionsData._classify_regime(20.0) == "ELEVATED"
        assert AlpacaOptionsData._classify_regime(29.9) == "ELEVATED"

    def test_high_vol(self) -> None:
        assert AlpacaOptionsData._classify_regime(30.0) == "HIGH_VOL"
        assert AlpacaOptionsData._classify_regime(50.0) == "HIGH_VOL"

    def test_boundary_15(self) -> None:
        assert AlpacaOptionsData._classify_regime(14.99) == "LOW_VOL"
        assert AlpacaOptionsData._classify_regime(15.0) == "NORMAL"


class TestGetMarketContext:
    @patch("services.alpaca_options_data.StockHistoricalDataClient")
    @patch("services.alpaca_options_data.OptionHistoricalDataClient")
    def test_normal_response(self, mock_opt_cls, mock_stock_cls) -> None:
        mock_stock_client = MagicMock()
        mock_stock_client.get_stock_snapshot.return_value = {
            "SPY": _mock_snapshot(525.40, 529.64),
        }
        mock_stock_cls.return_value = mock_stock_client

        mock_yf_ticker = MagicMock()
        mock_yf_ticker.fast_info = {"lastPrice": 17.51, "previousClose": 17.10}

        with patch("services.alpaca_options_data.yf") as mock_yf:
            mock_yf.Ticker.return_value = mock_yf_ticker
            client = AlpacaOptionsData()
            result = client.get_market_context()

        assert result["vix_level"] == 17.51
        assert result["spy_price"] == 525.40
        assert result["regime"] == "NORMAL"
        assert abs(result["vix_change_pct"] - 2.40) < 0.1
        assert abs(result["spy_change_pct"] - (-0.80)) < 0.1

    @patch("services.alpaca_options_data.StockHistoricalDataClient")
    @patch("services.alpaca_options_data.OptionHistoricalDataClient")
    def test_high_vol_regime(self, mock_opt_cls, mock_stock_cls) -> None:
        mock_stock_client = MagicMock()
        mock_stock_client.get_stock_snapshot.return_value = {
            "SPY": _mock_snapshot(480.0, 500.0),
        }
        mock_stock_cls.return_value = mock_stock_client

        mock_yf_ticker = MagicMock()
        mock_yf_ticker.fast_info = {"lastPrice": 35.0, "previousClose": 30.0}

        with patch("services.alpaca_options_data.yf") as mock_yf:
            mock_yf.Ticker.return_value = mock_yf_ticker
            client = AlpacaOptionsData()
            result = client.get_market_context()

        assert result["regime"] == "HIGH_VOL"
        assert result["vix_level"] == 35.0

    @patch("services.alpaca_options_data.StockHistoricalDataClient")
    @patch("services.alpaca_options_data.OptionHistoricalDataClient")
    def test_api_failure_returns_empty(self, mock_opt_cls, mock_stock_cls) -> None:
        mock_stock_client = MagicMock()
        mock_stock_client.get_stock_snapshot.side_effect = Exception("API down")
        mock_stock_cls.return_value = mock_stock_client

        with patch("services.alpaca_options_data.yf") as mock_yf:
            mock_yf.Ticker.side_effect = Exception("yfinance down")
            client = AlpacaOptionsData()
            result = client.get_market_context()

        assert result == {}

    @patch("services.alpaca_options_data.StockHistoricalDataClient")
    @patch("services.alpaca_options_data.OptionHistoricalDataClient")
    def test_vix_only_no_spy(self, mock_opt_cls, mock_stock_cls) -> None:
        mock_stock_client = MagicMock()
        mock_stock_client.get_stock_snapshot.return_value = {}
        mock_stock_cls.return_value = mock_stock_client

        mock_yf_ticker = MagicMock()
        mock_yf_ticker.fast_info = {"lastPrice": 22.0, "previousClose": 20.0}

        with patch("services.alpaca_options_data.yf") as mock_yf:
            mock_yf.Ticker.return_value = mock_yf_ticker
            client = AlpacaOptionsData()
            result = client.get_market_context()

        assert result["vix_level"] == 22.0
        assert result["regime"] == "ELEVATED"
        assert "spy_price" not in result

    @patch("services.alpaca_options_data.StockHistoricalDataClient")
    @patch("services.alpaca_options_data.OptionHistoricalDataClient")
    def test_zero_prev_close_no_division_error(self, mock_opt_cls, mock_stock_cls) -> None:
        mock_stock_client = MagicMock()
        mock_stock_client.get_stock_snapshot.return_value = {
            "SPY": _mock_snapshot(525.0, 0.0),
        }
        mock_stock_cls.return_value = mock_stock_client

        mock_yf_ticker = MagicMock()
        mock_yf_ticker.fast_info = {"lastPrice": 18.0, "previousClose": 0.0}

        with patch("services.alpaca_options_data.yf") as mock_yf:
            mock_yf.Ticker.return_value = mock_yf_ticker
            client = AlpacaOptionsData()
            result = client.get_market_context()

        assert result["vix_change_pct"] == 0.0
        assert result["spy_change_pct"] == 0.0


class TestFormatMarketContext:
    def test_none_returns_empty(self) -> None:
        from agents.orchestrator import Orchestrator
        assert Orchestrator._format_market_context(None) == ""

    def test_empty_dict_returns_empty(self) -> None:
        from agents.orchestrator import Orchestrator
        assert Orchestrator._format_market_context({}) == ""

    def test_full_context(self) -> None:
        from agents.orchestrator import Orchestrator
        ctx = {
            "vix_level": 18.50,
            "vix_change_pct": 2.3,
            "spy_price": 525.40,
            "spy_change_pct": -0.8,
            "regime": "NORMAL",
        }
        result = Orchestrator._format_market_context(ctx)
        assert "VIX" in result
        assert "VIXY" not in result
        assert "NORMAL regime" in result
        assert "SPY" in result
        assert "$525.40" in result
