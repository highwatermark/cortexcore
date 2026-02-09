"""
Multi-agent orchestrator using the Anthropic API.

Architecture:
  - Lead agent (orchestrator) coordinates 4 subagents
  - Each subagent has role-specific tools and system prompts
  - The orchestrator runs an agentic loop: send message -> handle tool calls -> repeat

Uses AsyncAnthropic so LLM calls don't block the event loop.
Includes retry with exponential backoff for transient API errors.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic

from agents.definitions import (
    executor_prompt,
    flow_scanner_prompt,
    orchestrator_prompt,
    position_manager_prompt,
    risk_manager_prompt,
)
from config.settings import get_settings
from core.logger import get_logger
from tools import TOOLS_BY_ROLE, dispatch_tool

log = get_logger("orchestrator")

# Retry config for transient API errors
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds


class AgentRunner:
    """Runs a single agent (lead or subagent) through an agentic tool-use loop."""

    def __init__(
        self,
        role: str,
        system_prompt: str,
        tools: list[dict],
        model: str | None = None,
        max_tokens: int | None = None,
        max_turns: int = 15,
    ) -> None:
        settings = get_settings()
        self.role = role
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model or settings.agent_model.subagent_model
        self.max_tokens = max_tokens or settings.agent_model.subagent_max_tokens
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
                # Non-retryable API errors (auth, bad request, etc.)
                log.error("api_error_non_retryable", role=self.role, status=e.status_code, error=str(e))
                raise

        raise last_error  # type: ignore[misc]

    async def run(self, user_message: str) -> str:
        """Run the agent with a user message and return the final text response.

        Loops through tool_use blocks until the model produces a text-only response
        or hits max_turns.
        """
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message},
        ]

        text_parts: list[str] = []

        for turn in range(self.max_turns):
            log.debug("agent_turn", role=self.role, turn=turn + 1)

            response = await self._call_api(messages)

            # Collect text and tool_use blocks
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

            # If no tool calls, we're done
            if not tool_calls:
                final_text = "\n".join(text_parts)
                log.info("agent_done", role=self.role, turns=turn + 1, response_len=len(final_text))
                return final_text

            # Add assistant message with all content blocks
            messages.append({"role": "assistant", "content": response.content})

            # Execute tools and collect results
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
        return "\n".join(text_parts) if text_parts else "[Agent reached max turns without final response]"


class Orchestrator:
    """The lead orchestrator that coordinates subagents for a trading cycle."""

    def __init__(self) -> None:
        settings = get_settings()

        # Lead agent has access to all tools
        self.lead = AgentRunner(
            role="orchestrator",
            system_prompt=orchestrator_prompt(),
            tools=TOOLS_BY_ROLE["orchestrator"],
            model=settings.agent_model.orchestrator_model,
            max_tokens=settings.agent_model.orchestrator_max_tokens,
            max_turns=20,
        )

        # Subagents — used for delegated tasks
        self.subagents = {
            "flow_scanner": AgentRunner(
                role="flow_scanner",
                system_prompt=flow_scanner_prompt(),
                tools=TOOLS_BY_ROLE["flow_scanner"],
            ),
            "position_manager": AgentRunner(
                role="position_manager",
                system_prompt=position_manager_prompt(),
                tools=TOOLS_BY_ROLE["position_manager"],
            ),
            "risk_manager": AgentRunner(
                role="risk_manager",
                system_prompt=risk_manager_prompt(),
                tools=TOOLS_BY_ROLE["risk_manager"],
            ),
            "executor": AgentRunner(
                role="executor",
                system_prompt=executor_prompt(),
                tools=TOOLS_BY_ROLE["executor"],
            ),
        }

    def _get_performance_context(self) -> str:
        """Build performance context string for agent prompts."""
        try:
            from analytics.performance import get_win_rate, get_max_drawdown, get_performance_summary
            from data.models import (
                IntentStatus,
                OrderIntent,
                PositionRecord,
                PositionStatus,
                TradeLog,
                get_session,
            )
            from datetime import datetime, timezone

            session = get_session()
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
                # Recent consecutive losses
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

            settings = get_settings()
            max_exec = settings.trading.max_executions_per_day
            max_pos = settings.trading.max_positions

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

    async def run_scan_cycle(self) -> str:
        """Run a complete scan cycle: check flow, evaluate positions, manage risk.

        This is called by the monitor loop on each tick.
        Returns a summary of actions taken.
        """
        perf_context = self._get_performance_context()
        prompt = (
            "Run a complete trading cycle:\n"
            "1. Scan for new unusual options flow signals\n"
            "2. Score any signals found and save them\n"
            "3. Check the current portfolio risk level\n"
            "4. For any signals scoring 7+, run pre-trade checks\n"
            "5. Check all open positions for exit triggers\n"
            "6. Execute any approved entries or exits\n"
            "7. Summarize what happened this cycle\n\n"
            "Be concise. Only execute trades that pass all checks."
            f"{perf_context}"
        )

        log.info("scan_cycle_start")
        result = await self.lead.run(prompt)
        log.info("scan_cycle_complete", result_len=len(result))
        return result

    async def run_position_check(self) -> str:
        """Focused position monitoring cycle."""
        prompt = (
            "Focus on position management:\n"
            "1. Get all open positions with current prices\n"
            "2. Check each position for exit triggers\n"
            "3. Execute any needed exits\n"
            "4. Summarize position status\n"
        )

        log.info("position_check_start")
        result = await self.lead.run(prompt)
        log.info("position_check_complete")
        return result

    async def run_risk_check(self) -> str:
        """Focused risk assessment."""
        prompt = (
            "Calculate the current portfolio risk score and provide a brief "
            "risk assessment. Include any warnings or concerns."
        )

        result = await self.lead.run(prompt)
        return result
