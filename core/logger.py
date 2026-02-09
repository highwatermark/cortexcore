"""
Structured logging configuration using structlog.

Features:
  - Human-readable console output (for journalctl)
  - JSON file output (logs/momentum.jsonl) for machine parsing
  - Correlation IDs: session_id (per startup), cycle_id (per tick)
  - contextvars for automatic propagation across async calls

Usage:
    from core.logger import get_logger, bind_session_id, bind_cycle_id
    log = get_logger("module_name")
    log.info("something happened", ticker="AAPL", score=8)
"""
from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import structlog


_configured = False


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure structlog + stdlib logging. Call once at startup."""
    global _configured
    if _configured:
        return
    _configured = True

    # Ensure log directory exists
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Stdlib root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Shared structlog processors
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    # Structlog configuration
    structlog.configure(
        processors=shared_processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Console handler — human-readable
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
    ))
    root.addHandler(console)

    # Log file — human-readable for journalctl compatibility
    file_handler = logging.FileHandler(log_path / "momentum.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=False),
    ))
    root.addHandler(file_handler)

    # JSON log file — machine-readable, one JSON object per line
    json_handler = logging.FileHandler(log_path / "momentum.jsonl")
    json_handler.setLevel(logging.DEBUG)
    json_handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
    ))
    root.addHandler(json_handler)

    # Bind session_id at startup
    bind_session_id()


def bind_session_id() -> str:
    """Bind a new session_id to all log events (survives for process lifetime)."""
    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    structlog.contextvars.bind_contextvars(session_id=session_id)
    return session_id


def bind_cycle_id(cycle_count: int) -> str:
    """Bind a cycle_id for the current monitoring tick."""
    cycle_id = f"cyc-{cycle_count:04d}"
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)
    return cycle_id


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a named structlog logger."""
    if not _configured:
        setup_logging()
    return structlog.get_logger(name)
