"""Shared test fixtures."""
from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure required env vars exist for Settings during tests."""
    defaults = {
        "ALPACA_API_KEY": "test-key",
        "ALPACA_SECRET_KEY": "test-secret",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "UW_API_KEY": "test-uw-key",
        "TELEGRAM_BOT_TOKEN": "test-bot-token",
        "TELEGRAM_ADMIN_ID": "12345",
        "DB_PATH": ":memory:",
        "SHADOW_MODE": "false",
        "PAPER_TRADING": "true",
    }
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)
