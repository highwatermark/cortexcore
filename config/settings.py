"""
Unified configuration via Pydantic BaseSettings.

Single source of truth — replaces agent-sdk/agent_config.py, agent-sdk/config.py, and root config.py.
All values validated at startup; missing/invalid env vars cause immediate failure.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import FrozenSet

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Nested config models
# ---------------------------------------------------------------------------

class ApiKeys(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alpaca_api_key: str = Field(..., alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(..., alias="ALPACA_SECRET_KEY")
    alpaca_base_url: str = Field("https://paper-api.alpaca.markets", alias="ALPACA_BASE_URL")
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    uw_api_key: str = Field(..., alias="UW_API_KEY")
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_ADMIN_ID")


class TradingLimits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADING_", extra="ignore")

    max_positions: int = Field(3, ge=1, le=20)
    max_position_value: float = Field(1000.0, gt=0)
    max_per_trade_pct: float = Field(0.20, gt=0, le=1.0)
    max_total_exposure_pct: float = Field(0.25, gt=0, le=1.0)
    max_total_options_exposure: float = Field(8000.0, gt=0)

    # Profit / loss
    profit_target_pct: float = Field(0.40, gt=0, le=5.0)
    stop_loss_pct: float = Field(0.35, gt=0, le=1.0)
    max_hold_dte: int = Field(5, ge=0)

    # Adaptive profit targets by DTE
    adaptive_target_dte_gt_14: float = Field(0.40, gt=0, le=5.0)
    adaptive_target_dte_7_to_14: float = Field(0.35, gt=0, le=5.0)
    adaptive_target_dte_3_to_7: float = Field(0.25, gt=0, le=5.0)
    adaptive_target_dte_lt_3: float = Field(0.15, gt=0, le=5.0)

    # Execution limits
    max_executions_per_day: int = Field(2, ge=1)

    # Premium
    min_premium_per_contract: float = Field(50.0, gt=0)
    max_premium_per_contract: float = Field(500.0, gt=0)

    # DTE
    min_dte: int = Field(14, ge=0)
    max_dte: int = Field(45, ge=1)

    # Liquidity
    max_spread_pct: float = Field(15.0, gt=0, le=100.0)
    min_volume: int = Field(10, ge=0)
    min_open_interest: int = Field(500, ge=0)
    min_bid_price: float = Field(0.05, ge=0)

    # Earnings
    earnings_blackout_days: int = Field(2, ge=0)

    # Limit orders
    use_limit_orders: bool = True
    limit_price_buffer_pct: float = Field(5.0, ge=0, le=50.0)

    @model_validator(mode="after")
    def dte_order(self) -> "TradingLimits":
        if self.min_dte >= self.max_dte:
            raise ValueError(f"min_dte ({self.min_dte}) must be < max_dte ({self.max_dte})")
        return self


class FlowScan(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLOW_", extra="ignore")

    # API-level filters
    min_premium: int = Field(100_000, ge=0)
    min_vol_oi_ratio: float = Field(1.5, ge=0)
    all_opening: bool = True
    min_dte: int = Field(14, ge=0)
    max_dte: int = Field(45, ge=1)
    issue_types: list[str] = Field(default_factory=lambda: ["Common Stock"])
    scan_limit: int = Field(30, ge=1, le=200)

    # Post-filter
    min_score: int = Field(7, ge=0, le=10)
    max_analyze: int = Field(10, ge=1)

    # Quality checks
    min_open_interest: int = Field(500, ge=0)
    max_strike_distance_pct: float = Field(0.10, gt=0, le=1.0)

    # Adaptive polling
    adaptive_scan_min_interval: int = Field(30, ge=5)
    adaptive_scan_max_interval: int = Field(180, ge=30)


class RiskFramework(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RISK_", extra="ignore")

    # Portfolio Greeks limits (per $100K equity)
    max_portfolio_delta_per_100k: float = Field(150.0, gt=0)
    max_portfolio_gamma_per_100k: float = Field(50.0, gt=0)
    max_portfolio_theta_daily_pct: float = Field(0.005, gt=0, le=1.0)
    max_portfolio_vega_pct: float = Field(0.01, gt=0, le=1.0)

    # Concentration limits
    max_sector_concentration: float = Field(0.40, gt=0, le=1.0)
    max_single_underlying_pct: float = Field(0.25, gt=0, le=1.0)
    max_correlated_exposure: float = Field(0.50, gt=0, le=1.0)

    # Entry gates
    min_conviction_for_entry: int = Field(80, ge=0, le=100)
    min_risk_capacity_pct: float = Field(0.20, ge=0, le=1.0)
    max_iv_rank_for_entry: int = Field(70, ge=0, le=100)
    require_trend_alignment: bool = True
    min_dte_for_entry: int = Field(14, ge=0)
    max_premium_per_contract: float = Field(500.0, gt=0)

    # Exit triggers
    profit_target_pct: float = Field(0.40, gt=0, le=5.0)
    stop_loss_pct: float = Field(0.35, gt=0, le=1.0)
    exit_on_thesis_invalidation: bool = True
    exit_on_gamma_risk: bool = True
    gamma_risk_dte_threshold: int = Field(5, ge=0)
    exit_on_concentration_breach: bool = True
    conviction_exit_threshold: int = Field(50, ge=0, le=100)
    conviction_hold_threshold: int = Field(65, ge=0, le=100)

    # Override conditions
    exceptional_conviction_threshold: int = Field(90, ge=0, le=100)
    exceptional_risk_allowance: float = Field(1.25, ge=1.0)

    # Risk score thresholds
    healthy_max: int = Field(30, ge=0, le=100)
    cautious_max: int = Field(50, ge=0, le=100)
    elevated_max: int = Field(70, ge=0, le=100)

    # Decision weights
    weight_conviction: float = Field(0.30, ge=0, le=1.0)
    weight_risk_capacity: float = Field(0.25, ge=0, le=1.0)
    weight_thesis_validity: float = Field(0.25, ge=0, le=1.0)
    weight_technical_alignment: float = Field(0.20, ge=0, le=1.0)


class MonitorConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", extra="ignore")

    poll_interval_seconds: int = Field(90, ge=5)
    greeks_snapshot_interval_seconds: int = Field(300, ge=30)

    # AI triggers
    ai_trigger_loss_pct: float = Field(0.35, gt=0, le=1.0)
    ai_trigger_profit_pct: float = Field(0.30, gt=0, le=1.0)
    ai_trigger_dte: int = Field(7, ge=0)
    ai_review_cooldown_minutes: int = Field(10, ge=1)

    # Greeks triggers
    gamma_risk_threshold: float = Field(0.08, gt=0)
    iv_crush_threshold_pct: float = Field(20.0, gt=0)

    # Auto-exit
    enable_auto_exit: bool = True
    max_auto_exits_per_day: int = Field(5, ge=1)

    # Alert dedup
    alert_cooldown_minutes: int = Field(30, ge=1)

    # Adaptive frequency
    at_risk_poll_interval_seconds: int = Field(15, ge=5)

    # Circuit breaker (consecutive errors)
    max_consecutive_errors: int = Field(5, ge=1)
    circuit_breaker_cooldown_seconds: int = Field(7200, ge=30)

    # Trading circuit breakers
    max_consecutive_losses: int = Field(2, ge=1)
    max_daily_loss_pct: float = Field(0.05, gt=0, le=1.0)
    max_weekly_loss_pct: float = Field(0.10, gt=0, le=1.0)
    loss_cooldown_minutes: int = Field(120, ge=1)

    # Market timing safety
    market_open_delay_minutes: int = Field(15, ge=0)
    market_close_buffer_minutes: int = Field(15, ge=0)


class MarketHours(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MARKET_", extra="ignore")

    open_hour: int = Field(9, ge=0, le=23)
    open_minute: int = Field(30, ge=0, le=59)
    close_hour: int = Field(16, ge=0, le=23)
    close_minute: int = Field(0, ge=0, le=59)
    timezone: str = "America/New_York"


class AgentModel(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    orchestrator_model: str = "claude-sonnet-4-20250514"
    orchestrator_max_tokens: int = Field(8192, gt=0)
    subagent_model: str = "claude-sonnet-4-20250514"
    subagent_max_tokens: int = Field(4096, gt=0)


# ---------------------------------------------------------------------------
# Excluded tickers — single source of truth
# ---------------------------------------------------------------------------

EXCLUDED_TICKERS: FrozenSet[str] = frozenset({
    # Index ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector ETFs
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLB", "XLC", "XLY", "XLP", "XLRE",
    # Commodities / Bonds
    "GLD", "SLV", "TLT", "HYG", "EEM", "EFA", "UNG",
    # Volatility products
    "VXX", "UVXY", "SVXY",
    # Leveraged ETFs
    "SQQQ", "TQQQ", "SPXU", "SPXL", "UPRO",
    # Meme / manipulation risk
    "AMC", "GME", "BBBY", "MULN", "HYMC", "MMAT", "ATER", "DWAC",
    # Low quality / penny risk
    "WISH", "PLTR",
    # Index options
    "SPXW", "SPX", "NDX", "XSP",
})


# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Sub-configs (populated from env + defaults)
    api: ApiKeys = Field(default_factory=ApiKeys)
    trading: TradingLimits = Field(default_factory=TradingLimits)
    flow: FlowScan = Field(default_factory=FlowScan)
    risk: RiskFramework = Field(default_factory=RiskFramework)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    market_hours: MarketHours = Field(default_factory=MarketHours)
    agent_model: AgentModel = Field(default_factory=AgentModel)

    # Database
    db_path: str = Field("data/momentum.db", alias="DB_PATH")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")

    # Execution mode
    shadow_mode: bool = Field(False, alias="SHADOW_MODE")
    paper_trading: bool = Field(True, alias="PAPER_TRADING")

    # Excluded tickers (not from env, static)
    excluded_tickers: FrozenSet[str] = EXCLUDED_TICKERS

    @model_validator(mode="after")
    def cross_field_checks(self) -> "Settings":
        # Stop/profit sanity: stop loss should not be wider than profit target would allow recovery
        if self.risk.stop_loss_pct > 0.80:
            raise ValueError("stop_loss_pct > 80% is unreasonably wide")
        return self

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).parent / "prompts"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — call once at startup, reuse everywhere."""
    return Settings()
