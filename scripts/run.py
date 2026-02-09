#!/usr/bin/env python3
"""
Main entry point for the Momentum Agent v2 trading system.

Usage:
    python -m scripts.run            # Start the monitoring loop
    python -m scripts.run --once     # Run a single scan cycle and exit
    python -m scripts.run --risk     # Run a risk check and exit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from core.logger import get_logger, setup_logging
from data.models import init_db
from monitor.loop import MonitorLoop
from agents.orchestrator import Orchestrator


def bootstrap() -> None:
    """Initialize all system components."""
    settings = get_settings()

    # Logging
    setup_logging(settings.log_level, settings.log_dir)
    log = get_logger("bootstrap")
    log.info(
        "bootstrap",
        paper=settings.paper_trading,
        shadow=settings.shadow_mode,
        model=settings.agent_model.orchestrator_model,
    )

    # Database
    db_path = settings.db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    log.info("db_initialized", path=db_path)


async def run_loop() -> None:
    """Run the continuous monitoring loop."""
    monitor = MonitorLoop()
    await monitor.start()


async def run_once() -> None:
    """Run a single scan cycle."""
    log = get_logger("run_once")
    orchestrator = Orchestrator()
    log.info("running_single_cycle")
    result = await orchestrator.run_scan_cycle()
    print("\n--- Scan Cycle Result ---")
    print(result)
    print("--- End ---\n")


async def run_risk() -> None:
    """Run a risk assessment."""
    log = get_logger("run_risk")
    orchestrator = Orchestrator()
    log.info("running_risk_check")
    result = await orchestrator.run_risk_check()
    print("\n--- Risk Assessment ---")
    print(result)
    print("--- End ---\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Momentum Agent v2 Trading System")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    parser.add_argument("--risk", action="store_true", help="Run a risk check and exit")
    parser.add_argument("--kill", action="store_true", help="Engage kill switch (halt all trading)")
    parser.add_argument("--unkill", action="store_true", help="Disengage kill switch (resume trading)")
    args = parser.parse_args()

    if args.kill:
        from core.killswitch import engage
        engage("Manual kill via --kill flag")
        print("KILL SWITCH ENGAGED — trading halted")
        return
    if args.unkill:
        from core.killswitch import disengage
        disengage()
        print("Kill switch disengaged — trading will resume")
        return

    bootstrap()

    if args.once:
        asyncio.run(run_once())
    elif args.risk:
        asyncio.run(run_risk())
    else:
        asyncio.run(run_loop())


if __name__ == "__main__":
    main()
