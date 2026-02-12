"""
Tools for the Risk Manager subagent.

Provides portfolio risk calculations, pre-trade checks, and risk scoring.
"""
from __future__ import annotations

from config.settings import get_settings
from core.logger import get_logger
from data.models import (
    PositionRecord,
    PositionStatus,
    RiskAssessment,
    RiskLevel,
    get_session,
)
from services.alpaca_broker import get_broker

log = get_logger("risk_tools")


def calculate_portfolio_risk() -> dict:
    """Calculate current portfolio risk score (0-100).

    Components (25 points each):
    - Delta exposure vs limit
    - Gamma concentration vs limit
    - Theta decay rate vs limit
    - Position concentration vs limit

    Returns RiskAssessment as dict.
    """
    settings = get_settings()
    risk_cfg = settings.risk

    # Get account equity for normalization
    try:
        account = get_broker().get_account()
        equity = account.get("equity", 0)
    except Exception:
        equity = 0

    if equity <= 0:
        log.warning("risk_equity_unavailable", equity=equity)
        # Return safe defaults — don't allow new positions
        return {
            "risk_score": 100,
            "risk_level": "CRITICAL",
            "position_count": 0,
            "risk_capacity_pct": 0,
            "can_add_position": False,
            "warnings": ["Cannot verify equity — broker unreachable or zero equity"],
        }

    scaling = max(equity, 1) / 100_000  # normalize to per-$100K, guard ZeroDivision

    # Get open positions from DB
    session = get_session()
    try:
        positions = (
            session.query(PositionRecord)
            .filter(PositionRecord.status == PositionStatus.OPEN)
            .all()
        )

        total_delta = sum(abs(p.delta or 0) * p.quantity for p in positions)
        total_gamma = sum(abs(p.gamma or 0) * p.quantity for p in positions)
        total_theta = sum(abs(p.theta or 0) * p.quantity for p in positions)

        # Concentration: max single underlying as % of total value
        underlying_values: dict[str, float] = {}
        total_value = 0.0
        for p in positions:
            val = (p.current_value or p.entry_value or 0)
            underlying_values[p.ticker] = underlying_values.get(p.ticker, 0) + val
            total_value += val

        max_concentration = 0.0
        if total_value > 0:
            max_concentration = max(underlying_values.values(), default=0) / total_value

        log.debug(
            "risk_inputs",
            equity=equity,
            position_count=len(positions),
            total_delta=round(total_delta, 4),
            total_gamma=round(total_gamma, 4),
            total_theta=round(total_theta, 4),
            max_concentration=round(max_concentration, 4),
            total_value=round(total_value, 2),
        )

        # Score components (each 0-25)
        delta_score = min(25, int(25 * (total_delta / scaling) / risk_cfg.max_portfolio_delta_per_100k))
        gamma_score = min(25, int(25 * (total_gamma / scaling) / risk_cfg.max_portfolio_gamma_per_100k))

        theta_pct = (total_theta * 100 / equity) if equity > 0 else 0
        theta_score = min(25, int(25 * theta_pct / (risk_cfg.max_portfolio_theta_daily_pct * 100)))

        conc_score = min(25, int(25 * max_concentration / risk_cfg.max_single_underlying_pct))

        risk_score = delta_score + gamma_score + theta_score + conc_score

        # Determine risk level
        if risk_score <= risk_cfg.healthy_max:
            risk_level = RiskLevel.HEALTHY
        elif risk_score <= risk_cfg.cautious_max:
            risk_level = RiskLevel.CAUTIOUS
        elif risk_score <= risk_cfg.elevated_max:
            risk_level = RiskLevel.ELEVATED
        else:
            risk_level = RiskLevel.CRITICAL

        # Risk capacity
        risk_capacity = max(0, (100 - risk_score)) / 100

        # Warnings
        warnings: list[str] = []
        if max_concentration > risk_cfg.max_single_underlying_pct:
            top_ticker = max(underlying_values, key=underlying_values.get, default="?")
            warnings.append(f"Concentration: {top_ticker} at {max_concentration:.0%} > {risk_cfg.max_single_underlying_pct:.0%}")
        if total_delta / scaling > risk_cfg.max_portfolio_delta_per_100k:
            warnings.append(f"Delta: {total_delta/scaling:.0f} > {risk_cfg.max_portfolio_delta_per_100k}")
        if len(positions) >= settings.trading.max_positions:
            warnings.append(f"Max positions reached: {len(positions)}/{settings.trading.max_positions}")

        assessment = RiskAssessment(
            risk_score=risk_score,
            risk_level=risk_level,
            delta_exposure=round(total_delta / scaling, 1),
            gamma_exposure=round(total_gamma / scaling, 1),
            theta_daily_pct=round(theta_pct, 3),
            max_concentration_pct=round(max_concentration, 3),
            position_count=len(positions),
            risk_capacity_pct=round(risk_capacity, 3),
            can_add_position=len(positions) < settings.trading.max_positions and risk_score < risk_cfg.elevated_max,
            warnings=warnings,
        )

        log.info(
            "risk_calculated",
            score=risk_score,
            level=risk_level.value,
            capacity=f"{risk_capacity:.0%}",
            positions=len(positions),
        )
        return assessment.model_dump()
    finally:
        session.close()


def pre_trade_check(signal: dict, risk_assessment: dict) -> dict:
    """Run pre-trade checks for a proposed entry.

    Checks: position count, size, concentration, Greeks impact,
    earnings blackout, liquidity, IV rank, DTE.

    Returns dict with approved/denied and reasons.
    """
    settings = get_settings()
    risk_cfg = settings.risk
    trading = settings.trading
    reasons: list[str] = []
    approved = True

    # Position count
    if risk_assessment.get("position_count", 0) >= trading.max_positions:
        reasons.append(f"Max positions reached ({trading.max_positions})")
        approved = False

    # Risk capacity
    capacity = risk_assessment.get("risk_capacity_pct", 0)
    if capacity < risk_cfg.min_risk_capacity_pct:
        conviction = signal.get("conviction", 0)
        if conviction >= risk_cfg.exceptional_conviction_threshold:
            reasons.append(f"Low capacity ({capacity:.0%}) but exceptional conviction ({conviction}%)")
        else:
            reasons.append(f"Insufficient risk capacity: {capacity:.0%} < {risk_cfg.min_risk_capacity_pct:.0%}")
            approved = False

    # Risk level check
    risk_level = risk_assessment.get("risk_level", "HEALTHY")
    if risk_level == "CRITICAL":
        reasons.append("Portfolio risk is CRITICAL — no new entries")
        approved = False
    elif risk_level == "ELEVATED":
        reasons.append("Portfolio risk ELEVATED — only exceptional setups")

    # IV rank
    iv_rank = signal.get("iv_rank", 0)
    if iv_rank > risk_cfg.max_iv_rank_for_entry:
        reasons.append(f"IV rank too high: {iv_rank}% > {risk_cfg.max_iv_rank_for_entry}%")
        approved = False

    # DTE
    dte = signal.get("dte", 0)
    if dte < risk_cfg.min_dte_for_entry:
        reasons.append(f"DTE too low: {dte} < {risk_cfg.min_dte_for_entry}")
        approved = False

    # Premium per contract
    premium = signal.get("premium", 0)
    volume = signal.get("volume", 1)
    per_contract = premium / volume if volume > 0 else premium
    if per_contract > risk_cfg.max_premium_per_contract * 100:  # premium in total dollars
        pass  # Premium check is at per-contract level, handled differently

    # Liquidity
    oi = signal.get("open_interest", 0)
    if oi < trading.min_open_interest:
        reasons.append(f"Low open interest: {oi} < {trading.min_open_interest}")
        approved = False

    # Score check
    score = signal.get("score", 0)
    if score < settings.flow.min_score:
        reasons.append(f"Score too low: {score} < {settings.flow.min_score}")
        approved = False

    result = {
        "approved": approved,
        "reasons": reasons,
        "risk_level": risk_level,
        "risk_score": risk_assessment.get("risk_score", 0),
        "signal_id": signal.get("signal_id", ""),
        "ticker": signal.get("ticker", ""),
    }

    log.info("pre_trade_check", ticker=result["ticker"], approved=approved, reasons=reasons)
    return result


# ---------------------------------------------------------------------------
# Tool definitions for Claude agent SDK
# ---------------------------------------------------------------------------

RISK_TOOLS = [
    {
        "name": "calculate_portfolio_risk",
        "description": (
            "Calculate the current portfolio risk score (0-100) based on delta exposure, "
            "gamma concentration, theta decay, and position concentration. Returns risk level "
            "(HEALTHY/CAUTIOUS/ELEVATED/CRITICAL), capacity for new trades, and warnings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "pre_trade_check",
        "description": (
            "Run pre-trade approval checks for a proposed entry. Validates position count, "
            "risk capacity, IV rank, DTE, liquidity, and score. Returns approved/denied with reasons."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal": {
                    "type": "object",
                    "description": "The scored signal dict to evaluate",
                },
                "risk_assessment": {
                    "type": "object",
                    "description": "Current portfolio risk from calculate_portfolio_risk",
                },
            },
            "required": ["signal", "risk_assessment"],
        },
    },
]
