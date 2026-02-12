"""Tests for configuration settings."""
from __future__ import annotations

import pytest
from config.settings import Settings, get_settings, EXCLUDED_TICKERS


class TestSettings:
    def test_settings_load(self) -> None:
        settings = get_settings()
        assert settings.paper_trading is True
        assert settings.shadow_mode is False

    def test_api_keys_present(self) -> None:
        settings = get_settings()
        assert settings.api.alpaca_api_key
        assert settings.api.anthropic_api_key
        assert settings.api.uw_api_key

    def test_trading_limits_defaults(self) -> None:
        settings = get_settings()
        assert settings.trading.max_positions == 3
        assert settings.trading.max_position_value == 1000.0
        assert settings.trading.max_per_trade_pct == 0.20
        assert settings.trading.max_total_exposure_pct == 0.25
        assert settings.trading.profit_target_pct == 0.40
        assert settings.trading.stop_loss_pct == 0.35
        assert settings.trading.max_hold_dte == 5
        assert settings.trading.max_executions_per_day == 2
        assert settings.trading.max_spread_pct == 15.0

    def test_trading_circuit_breakers(self) -> None:
        settings = get_settings()
        assert settings.monitor.max_consecutive_losses == 2
        assert settings.monitor.max_daily_loss_pct == 0.05
        assert settings.monitor.max_weekly_loss_pct == 0.10
        assert settings.monitor.loss_cooldown_minutes == 120
        assert settings.monitor.market_open_delay_minutes == 15
        assert settings.monitor.market_close_buffer_minutes == 15
        assert settings.monitor.circuit_breaker_cooldown_seconds == 7200

    def test_adaptive_profit_targets(self) -> None:
        settings = get_settings()
        assert settings.trading.adaptive_target_dte_gt_14 == 0.40
        assert settings.trading.adaptive_target_dte_7_to_14 == 0.35
        assert settings.trading.adaptive_target_dte_3_to_7 == 0.25
        assert settings.trading.adaptive_target_dte_lt_3 == 0.15

    def test_flow_scan_defaults(self) -> None:
        settings = get_settings()
        assert settings.flow.min_score == 7
        assert settings.flow.min_premium == 100_000
        assert settings.flow.min_vol_oi_ratio == 1.5

    def test_risk_framework_defaults(self) -> None:
        settings = get_settings()
        assert settings.risk.healthy_max == 30
        assert settings.risk.cautious_max == 50
        assert settings.risk.elevated_max == 70

    def test_excluded_tickers(self) -> None:
        assert "SPY" in EXCLUDED_TICKERS
        assert "QQQ" in EXCLUDED_TICKERS
        assert "GME" in EXCLUDED_TICKERS
        assert "AAPL" not in EXCLUDED_TICKERS

    def test_market_hours(self) -> None:
        settings = get_settings()
        assert settings.market_hours.open_hour == 6
        assert settings.market_hours.open_minute == 30
        assert settings.market_hours.close_hour == 13
        assert settings.market_hours.timezone == "America/Los_Angeles"
