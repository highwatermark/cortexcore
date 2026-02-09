"""
Agent tool registry.

Maps tool names to their implementations and provides tool definition lists
for each subagent role.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from tools.execution_tools import (
    EXECUTION_TOOLS,
    calculate_position_size,
    execute_entry,
    execute_exit,
    get_account_info,
    reconcile_orders,
)
from tools.flow_tools import FLOW_TOOLS, save_signal, scan_flow, score_signal, send_scan_report
from tools.position_tools import POSITION_TOOLS, check_exit_triggers, get_open_positions
from tools.risk_tools import RISK_TOOLS, calculate_portfolio_risk, pre_trade_check

# ---------------------------------------------------------------------------
# Tool name -> callable mapping
# ---------------------------------------------------------------------------

TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    # Flow tools
    "scan_flow": scan_flow,
    "score_signal": score_signal,
    "save_signal": save_signal,
    "send_scan_report": send_scan_report,
    # Position tools
    "get_open_positions": get_open_positions,
    "check_exit_triggers": check_exit_triggers,
    # Risk tools
    "calculate_portfolio_risk": calculate_portfolio_risk,
    "pre_trade_check": pre_trade_check,
    # Execution tools
    "calculate_position_size": calculate_position_size,
    "execute_entry": execute_entry,
    "execute_exit": execute_exit,
    "get_account_info": get_account_info,
}

# Tools grouped by subagent role
TOOLS_BY_ROLE: dict[str, list[dict]] = {
    "flow_scanner": FLOW_TOOLS,
    "position_manager": POSITION_TOOLS,
    "risk_manager": RISK_TOOLS,
    "executor": EXECUTION_TOOLS,
    "orchestrator": FLOW_TOOLS + POSITION_TOOLS + RISK_TOOLS + EXECUTION_TOOLS,
}


async def dispatch_tool(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call and return the JSON result string.

    Handles both sync and async tool functions.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    import asyncio
    import inspect

    try:
        if inspect.iscoroutinefunction(handler):
            result = await handler(**arguments)
        else:
            result = handler(**arguments)

        if isinstance(result, (dict, list)):
            return json.dumps(result, default=str)
        return str(result)
    except Exception as e:
        return json.dumps({"error": f"Tool {name} failed: {str(e)}"})
