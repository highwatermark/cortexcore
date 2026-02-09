"""Tests for core utility functions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.utils import OccSymbol, calc_dte, parse_occ_symbol


class TestParseOccSymbol:
    def test_standard_call(self) -> None:
        result = parse_occ_symbol("AAPL250321C00175000")
        assert result is not None
        assert result.ticker == "AAPL"
        assert result.expiration == "2025-03-21"
        assert result.option_type == "CALL"
        assert result.strike == 175.0
        assert result.raw == "AAPL250321C00175000"

    def test_standard_put(self) -> None:
        result = parse_occ_symbol("MSFT260115P00400000")
        assert result is not None
        assert result.ticker == "MSFT"
        assert result.expiration == "2026-01-15"
        assert result.option_type == "PUT"
        assert result.strike == 400.0

    def test_fractional_strike(self) -> None:
        result = parse_occ_symbol("SPY250718C00542500")
        assert result is not None
        assert result.strike == 542.5

    def test_small_strike(self) -> None:
        result = parse_occ_symbol("F250321C00012000")
        assert result is not None
        assert result.ticker == "F"
        assert result.strike == 12.0

    def test_six_letter_ticker(self) -> None:
        result = parse_occ_symbol("GOOGLL260620C01500000")
        assert result is not None
        assert result.ticker == "GOOGLL"
        assert result.strike == 1500.0

    def test_lowercase_normalized(self) -> None:
        result = parse_occ_symbol("aapl250321c00175000")
        assert result is not None
        assert result.ticker == "AAPL"
        assert result.option_type == "CALL"

    def test_whitespace_stripped(self) -> None:
        result = parse_occ_symbol("  AAPL250321C00175000  ")
        assert result is not None
        assert result.ticker == "AAPL"

    def test_invalid_empty_string(self) -> None:
        assert parse_occ_symbol("") is None

    def test_invalid_stock_symbol(self) -> None:
        assert parse_occ_symbol("AAPL") is None

    def test_invalid_bad_date(self) -> None:
        assert parse_occ_symbol("AAPL259921C00175000") is None

    def test_invalid_no_cp_indicator(self) -> None:
        assert parse_occ_symbol("AAPL250321X00175000") is None

    def test_returns_named_tuple(self) -> None:
        result = parse_occ_symbol("NVDA250321C00950000")
        assert isinstance(result, OccSymbol)
        assert result.ticker == result[0]
        assert result.expiration == result[1]


class TestCalcDte:
    def test_future_expiration(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        dte = calc_dte(future)
        assert 29 <= dte <= 30

    def test_past_expiration(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        dte = calc_dte(past)
        assert dte == 0

    def test_today_expiration(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dte = calc_dte(today)
        assert dte == 0

    def test_empty_string(self) -> None:
        assert calc_dte("") == 0

    def test_invalid_date(self) -> None:
        assert calc_dte("not-a-date") == 0
