"""
Orchestrator — deterministic pipeline + Claude for trade decisions only.

Flow (per cycle):
  1. Monitor loop calls deterministic functions directly (scan, score, save,
     risk, positions, exit triggers, pre-trade checks) — zero Claude API calls.
  2. Only when signals score 7+ AND pass pre-trade checks, Claude is called
     ONCE to evaluate entries (thesis, conviction, sizing, execute or skip).
  3. Only when exit triggers fire on open positions, Claude is called ONCE
     to confirm exits.

This keeps Claude API usage to 0-2 calls per cycle instead of 4-10+.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic

from config.settings import get_settings
from core.logger import get_logger
from tools import dispatch_tool

log = get_logger("orchestrator")

# Retry config for transient API errors
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds

# ---------------------------------------------------------------------------
# Tools Claude gets for decisions — execution only, no scanning/scoring
# ---------------------------------------------------------------------------

DECISION_TOOLS = [
    # {
    #     "name": "calculate_position_size",
    #     "description": (
    #         "Calculate the maximum number of contracts for a new position based on "
    #         "current equity, existing exposure, and risk limits. Returns max_contracts "
    #         "and the limiting factor. Call this BEFORE execute_entry."
    #     ),
    #     "input_schema": {
    #         "type": "object",
    #         "properties": {
    #             "option_price": {
    #                 "type": "number",
    #                 "description": "The option premium per contract (e.g., 2.50 for $250/contract)",
    #             },
    #         },
    #         "required": ["option_price"],
    #     },
    # },
    {
        "name": "execute_entry",
        "description": (
            "Execute a BUY limit order for an options contract. Runs the safety gate, "
            "submits to Alpaca, polls for fill, creates position record, sends Telegram alert. "
            "Only call this when you have decided to trade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_id": {"type": "string", "description": "The signal ID"},
                "ticker": {"type": "string", "description": "Stock ticker"},
                "option_symbol": {"type": "string", "description": "Full OCC option symbol"},
                "side": {"type": "string", "enum": ["BUY", "SELL"], "description": "Order side"},
                "quantity": {"type": "integer", "description": "Number of contracts"},
                "limit_price": {"type": "number", "description": "Limit price per contract"},
                "thesis": {"type": "string", "description": "1-2 sentence entry thesis"},
                "conviction": {"type": "integer", "description": "Conviction score 0-100"},
                "iv_rank": {"type": "number", "description": "IV rank (0-100)"},
                "dte": {"type": "integer", "description": "Days to expiration"},
            },
            "required": ["signal_id", "ticker", "option_symbol", "side", "quantity", "limit_price"],
        },
    },
    {
        "name": "execute_exit",
        "description": (
            "Exit an open position. Submits sell order, polls for fill, records P&L, "
            "sends Telegram alert."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position_id": {"type": "string", "description": "Position ID to exit"},
                "reason": {"type": "string", "description": "Exit reason"},
                "use_market": {"type": "boolean", "description": "Market order (emergencies only)"},
            },
            "required": ["position_id"],
        },
    },
    {
        "name": "get_account_info",
        "description": "Get broker account: equity, buying power, cash, portfolio value.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# ---------------------------------------------------------------------------
# System prompts — focused on decision-making, not orchestration
# ---------------------------------------------------------------------------

ENTRY_DECISION_SYSTEM = """\
You are an options trader evaluating whether to enter trades based on unusual options flow.

You receive pre-computed data from a deterministic pipeline:
- High-scoring flow signals (already scored 7+ out of 10 and passed pre-trade checks)
- Current portfolio risk assessment
- Open positions with P&L
- Account equity and position sizing constraints

Your job is to decide for each signal: TRADE or SKIP.

For TRADE signals:
1. Use the SIZING CONSTRAINTS provided to calculate quantity yourself:
   quantity = floor(min(per_trade_cap, position_value_cap, remaining_capacity) / (option_price * 100))
   If quantity is 0, SKIP (option too expensive for current limits).
2. Call execute_entry directly with your conviction (0-100), thesis, and quantity.

RISK RULES (non-negotiable):
- Never enter if risk_level is CRITICAL
- If risk_level is ELEVATED, only exceptional setups (conviction >= 90)
- Consider correlation with existing positions (don't double up on same sector)

MARKET REGIME (from VIX level):
- LOW_VOL: Standard entries. Flow signals likely directional conviction.
- NORMAL: Standard entries. No adjustment needed.
- ELEVATED: Tighten sizing by 50%. Flow may be hedging, not conviction. Require higher conviction (80+).
- HIGH_VOL: Extreme caution. Most flow is likely hedging. Only enter with conviction 95+ and clear directional thesis.

Keep your reasoning brief. Focus on the decision.
"""

EXIT_DECISION_SYSTEM = """\
You are an options trader evaluating whether to exit positions that have triggered exit conditions.

You receive pre-computed data:
- Positions with triggered exit conditions, urgency levels, and trigger details
- Current portfolio risk assessment

For each position, decide: EXIT or HOLD.

HARD RULES (always exit, no discretion):
- DTE <= 5 (mandatory expiration risk)
- Stop loss hit (P&L <= -35%)

DISCRETIONARY (you decide):
- Profit target hit — consider momentum, is there more to gain?
- Gamma risk — evaluate if the remaining edge justifies the risk
- Theta acceleration — is time decay eating the position?
- Conviction drop — has the thesis been invalidated?

MARKET REGIME considerations:
- In ELEVATED/HIGH_VOL: lower profit targets (take profits sooner), tighten stops.
- In LOW_VOL: can afford to let winners run longer.

For EXIT: call execute_exit with position_id and reason.
For HOLD: explain briefly why you're holding despite the trigger.

Keep reasoning brief.
"""


class AgentRunner:
    """Runs a Claude agent through an agentic tool-use loop."""

    def __init__(
        self,
        role: str,
        system_prompt: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        max_turns: int = 8,
    ) -> None:
        settings = get_settings()
        self.role = role
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model or settings.agent_model.orchestrator_model
        self.max_tokens = max_tokens or settings.agent_model.orchestrator_max_tokens
        self.max_turns = max_turns
        self._client = anthropic.AsyncAnthropic(api_key=settings.api.anthropic_api_key)

    async def _call_api(self, messages: list[dict[str, Any]]) -> anthropic.types.Message:
        """Call the Anthropic API with retry and exponential backoff."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    tools=self.tools,
                    messages=messages,
                )
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                last_error = e
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning("api_retry", role=self.role, attempt=attempt + 1, delay=delay, error=str(e))
                await asyncio.sleep(delay)
            except anthropic.APIStatusError as e:
                log.error("api_error_non_retryable", role=self.role, status=e.status_code, error=str(e))
                raise

        raise last_error  # type: ignore[misc]

    async def run(self, user_message: str) -> str:
        """Run the agent and return the final text response."""
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message},
        ]

        text_parts: list[str] = []

        for turn in range(self.max_turns):
            log.debug("agent_turn", role=self.role, turn=turn + 1)
            response = await self._call_api(messages)

            text_parts = []
            tool_calls: list[dict] = []

            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            if not tool_calls:
                final_text = "\n".join(text_parts)
                log.info("agent_done", role=self.role, turns=turn + 1, response_len=len(final_text))
                return final_text

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tc in tool_calls:
                log.info("tool_call", role=self.role, tool=tc["name"], args_keys=list(tc["input"].keys()))
                result_str = await dispatch_tool(tc["name"], tc["input"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": result_str,
                })

            messages.append({"role": "user", "content": tool_results})

        log.warning("agent_max_turns", role=self.role, max_turns=self.max_turns)
        return "\n".join(text_parts) if text_parts else "[Agent reached max turns]"


class Orchestrator:
    """Deterministic pipeline + Claude for trade decisions only.

    The monitor loop calls deterministic methods directly. Claude is invoked
    only via evaluate_entries() and evaluate_exits().
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def _make_agent(self, system: str) -> AgentRunner:
        """Create a short-lived agent for a single decision."""
        return AgentRunner(
            role="trade_decision",
            system_prompt=system,
            tools=DECISION_TOOLS,
            model=self._settings.agent_model.orchestrator_model,
            max_tokens=self._settings.agent_model.orchestrator_max_tokens,
            max_turns=8,
        )

    async def evaluate_entries(
        self,
        passing_signals: list[tuple[dict, dict, dict]],
        risk_assessment: dict,
        positions: list[dict],
        perf_context: str,
        market_context: dict | None = None,
    ) -> str:
        """Call Claude ONCE to decide on entries for signals that passed all checks.

        Args:
            passing_signals: list of (signal, score_result, pre_trade_result) tuples
            risk_assessment: portfolio risk from calculate_portfolio_risk()
            positions: open positions from get_open_positions()
            perf_context: performance context string
            market_context: VIX/SPY context from get_market_context()

        Returns:
            Claude's response text (reasoning + any tool calls it made).
        """
        signals_text = []
        for sig, score, ptc in passing_signals:
            signals_text.append(
                f"  {sig['ticker']} {sig.get('option_type', '?')} ${sig.get('strike', 0):.0f} "
                f"exp {sig.get('expiration', '?')} DTE={sig.get('dte', 0)}\n"
                f"    Premium: ${sig.get('premium', 0):,.0f} | Vol/OI: {sig.get('vol_oi_ratio', 0)} | "
                f"Order type: {sig.get('order_type', '?')}\n"
                f"    Score: {score['score']}/10 — {score['breakdown']}\n"
                f"    Direction: {sig.get('directional_pct', 0):.0%} {sig.get('directional_side', '')} side\n"
                f"    Underlying: ${sig.get('underlying_price', 0):,.2f}\n"
                f"    Signal ID: {sig.get('signal_id', '')}\n"
                f"    Pre-trade: {'APPROVED' if ptc.get('approved') else 'DENIED'} "
                f"{', '.join(ptc.get('reasons', [])) or '(no issues)'}"
            )

        risk_text = (
            f"Risk score: {risk_assessment.get('risk_score', 0)}/100 "
            f"({risk_assessment.get('risk_level', 'UNKNOWN')})\n"
            f"  Risk capacity: {risk_assessment.get('risk_capacity_pct', 0):.0%}\n"
            f"  Open positions: {risk_assessment.get('position_count', 0)}/{self._settings.trading.max_positions}\n"
            f"  Delta exposure: {risk_assessment.get('delta_exposure', 0)}\n"
            f"  Theta daily: {risk_assessment.get('theta_daily_pct', 0):.3%}\n"
            f"  Warnings: {', '.join(risk_assessment.get('warnings', [])) or 'none'}"
        )

        if positions:
            pos_lines = []
            for p in positions:
                pos_lines.append(
                    f"  {p['ticker']} {p.get('action', '?')} ${p.get('strike', 0):.0f} "
                    f"exp {p.get('expiration', '?')} DTE={p.get('dte_remaining', 0)}\n"
                    f"    P&L: {p.get('pnl_pct', 0):+.1f}% (${p.get('pnl_dollars', 0):+.2f}) "
                    f"Qty: {p.get('quantity', 0)} @ ${p.get('entry_price', 0):.2f}"
                )
            positions_text = "\n".join(pos_lines)
        else:
            positions_text = "  (none)"

        market_text = self._format_market_context(market_context)

        # Pre-fetch sizing constraints so Claude can compute quantity inline
        sizing_text = self._get_sizing_context(positions)

        prompt = (
            f"SIGNALS THAT PASSED ALL CHECKS (score 7+ and pre-trade approved):\n"
            f"{''.join(s + chr(10) for s in signals_text)}\n"
            f"PORTFOLIO RISK:\n{risk_text}\n\n"
            f"OPEN POSITIONS:\n{positions_text}\n"
            f"{sizing_text}\n"
            f"{perf_context}\n"
            f"{market_text}"
            f"For each signal, decide TRADE or SKIP. For trades, compute quantity from "
            f"the sizing constraints above and call execute_entry directly."
        )

        agent = self._make_agent(ENTRY_DECISION_SYSTEM)
        log.info("claude_entry_evaluation", signals=len(passing_signals))
        result = await agent.run(prompt)
        log.info("claude_entry_done", response_len=len(result))
        return result

    async def evaluate_exits(
        self,
        triggered_positions: list[tuple[dict, dict]],
        risk_assessment: dict,
        market_context: dict | None = None,
    ) -> str:
        """Call Claude ONCE to decide on exits for positions with triggers.

        Args:
            triggered_positions: list of (position_dict, trigger_result) tuples
            risk_assessment: portfolio risk from calculate_portfolio_risk()
            market_context: VIX/SPY context from get_market_context()

        Returns:
            Claude's response text.
        """
        triggers_text = []
        for pos, triggers in triggered_positions:
            triggers_text.append(
                f"  {pos['ticker']} {pos.get('action', '?')} ${pos.get('strike', 0):.0f} "
                f"exp {pos.get('expiration', '?')} DTE={pos.get('dte_remaining', 0)}\n"
                f"    P&L: {pos.get('pnl_pct', 0):+.1f}% (${pos.get('pnl_dollars', 0):+.2f})\n"
                f"    Entry: ${pos.get('entry_price', 0):.2f} → Current: ${pos.get('current_price', 0):.2f}\n"
                f"    Triggers: {', '.join(triggers.get('triggers', []))}\n"
                f"    Urgency: {triggers.get('urgency', 'low')}\n"
                f"    Thesis: {pos.get('entry_thesis', 'n/a')}\n"
                f"    Position ID: {pos['position_id']}"
            )

        market_text = self._format_market_context(market_context)

        prompt = (
            f"POSITIONS WITH EXIT TRIGGERS:\n"
            f"{''.join(t + chr(10) for t in triggers_text)}\n"
            f"Portfolio risk: {risk_assessment.get('risk_score', 0)}/100 "
            f"({risk_assessment.get('risk_level', 'UNKNOWN')})\n\n"
            f"{market_text}"
            f"For each position, decide EXIT or HOLD. "
            f"For critical urgency (DTE mandatory, stop loss), always EXIT."
        )

        agent = self._make_agent(EXIT_DECISION_SYSTEM)
        log.info("claude_exit_evaluation", positions=len(triggered_positions))
        result = await agent.run(prompt)
        log.info("claude_exit_done", response_len=len(result))
        return result

    @staticmethod
    def _format_market_context(market_context: dict | None) -> str:
        if not market_context:
            return ""
        regime = market_context.get("regime", "UNKNOWN")
        regime_notes = {
            "LOW_VOL": "Calm conditions — standard entries OK, let winners run.",
            "NORMAL": "Typical conditions — no adjustment needed.",
            "ELEVATED": "Heightened volatility — tighten sizing, flow may be hedging.",
            "HIGH_VOL": "Extreme volatility — most flow is hedging, extreme caution.",
        }
        vix = market_context.get("vix_level")
        vix_chg = market_context.get("vix_change_pct", 0)
        spy = market_context.get("spy_price")
        spy_chg = market_context.get("spy_change_pct", 0)
        lines = ["MARKET CONTEXT:"]
        if vix is not None:
            lines.append(f"  VIX: {vix:.2f} ({vix_chg:+.1f}%) — {regime} regime")
        if spy is not None:
            lines.append(f"  SPY: ${spy:.2f} ({spy_chg:+.1f}%)")
        lines.append(f"  Regime note: {regime_notes.get(regime, 'Unknown regime')}")
        return "\n".join(lines) + "\n\n"

    def _get_sizing_context(self, positions: list[dict]) -> str:
        """Pre-compute position sizing constraints for Claude."""
        trading = self._settings.trading
        try:
            from services.alpaca_broker import AlpacaBroker
            account = AlpacaBroker().get_account()
            equity = account.get("equity", 0)
        except Exception:
            return "SIZING CONSTRAINTS:\n  (equity unavailable — skip all trades)\n"

        current_exposure = sum((p.get("entry_value", 0) or 0) for p in positions)
        max_total = equity * trading.max_total_exposure_pct
        remaining_capacity = max(0, max_total - current_exposure)
        per_trade_cap = equity * trading.max_per_trade_pct

        return (
            f"SIZING CONSTRAINTS:\n"
            f"  Equity: ${equity:,.0f}\n"
            f"  Per-trade cap (20% equity): ${per_trade_cap:,.0f}\n"
            f"  Position value cap: ${trading.max_position_value:,.0f}\n"
            f"  Remaining exposure capacity: ${remaining_capacity:,.0f}\n"
            f"  Formula: quantity = floor(min(per_trade_cap, position_value_cap, remaining_capacity) / (option_price × 100))\n"
            f"  If quantity = 0, the option is too expensive — SKIP.\n"
        )

    def get_performance_context(self) -> str:
        """Build performance context string for Claude prompts."""
        try:
            from analytics.performance import get_win_rate, get_max_drawdown
            from data.models import (
                IntentStatus,
                OrderIntent,
                PositionRecord,
                PositionStatus,
                TradeLog,
                get_session,
            )
            from core.utils import trading_today

            session = get_session()
            try:
                today = trading_today()
                trades_today = (
                    session.query(OrderIntent)
                    .filter(
                        OrderIntent.idempotency_key.like("entry-%"),
                        OrderIntent.status == IntentStatus.EXECUTED,
                        OrderIntent.executed_at >= today,
                    )
                    .count()
                )
                open_positions = (
                    session.query(PositionRecord)
                    .filter(PositionRecord.status == PositionStatus.OPEN)
                    .count()
                )
                recent = (
                    session.query(TradeLog)
                    .order_by(TradeLog.closed_at.desc())
                    .limit(5)
                    .all()
                )
                consecutive_losses = 0
                for t in recent:
                    if t.pnl_dollars < 0:
                        consecutive_losses += 1
                    else:
                        break
            finally:
                session.close()

            win_rate = get_win_rate(30)
            drawdown = get_max_drawdown(30)

            max_exec = self._settings.trading.max_executions_per_day
            max_pos = self._settings.trading.max_positions

            return (
                f"\nPERFORMANCE CONTEXT:\n"
                f"- Win rate (30d): {win_rate:.0%}\n"
                f"- Current drawdown: {drawdown:.1%}\n"
                f"- Consecutive losses: {consecutive_losses}\n"
                f"- Trades today: {trades_today} / {max_exec} max\n"
                f"- Open positions: {open_positions} / {max_pos} max\n"
            )
        except Exception as e:
            log.warning("performance_context_error", error=str(e))
            return ""
