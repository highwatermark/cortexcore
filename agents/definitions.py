"""
Agent prompt definitions — loaded from external markdown files.

Replaces 449 lines of inline prompt strings from agent-sdk/agents/definitions.py.
"""
from __future__ import annotations

import functools
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"


@functools.lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Load a prompt from config/prompts/{name}.md.

    Raises FileNotFoundError if the prompt file doesn't exist.
    """
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()


# Convenience accessors
def orchestrator_prompt() -> str:
    return load_prompt("orchestrator")


def flow_scanner_prompt() -> str:
    return load_prompt("flow_scanner")


def position_manager_prompt() -> str:
    return load_prompt("position_manager")


def risk_manager_prompt() -> str:
    return load_prompt("risk_manager")


def executor_prompt() -> str:
    return load_prompt("executor")
