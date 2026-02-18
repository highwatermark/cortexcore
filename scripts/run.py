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
    """Run a single scan cycle using the same pipeline as the monitor loop."""
    from agents.orchestrator import Orchestrator
    from tools.flow_tools import scan_flow, score_signal, save_signal, send_scan_report
    from tools.position_tools import get_open_positions, check_exit_triggers
    from tools.risk_tools import calculate_portfolio_risk, pre_trade_check

    log = get_logger("run_once")
    orchestrator = Orchestrator()
    log.info("running_single_cycle")

    # Step 1: Scan flow
    signals = await scan_flow()
    print(f"\n--- Scan Cycle Result ---")
    print(f"Signals fetched: {len(signals)}")

    # Step 2: Score each signal
    scored: list[tuple[dict, dict]] = []
    for sig in signals:
        result = score_signal(sig)
        save_signal(sig, result)
        scored.append((sig, result))
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}] {sig.get('ticker', '?')} {sig.get('option_type', '?')} "
              f"${sig.get('strike', 0):.0f} DTE={sig.get('dte', 0)} "
              f"Score={result['score']}/10 — {result['breakdown']}")

    # Step 3: Send Telegram scan report
    if scored:
        try:
            await send_scan_report()
        except Exception as e:
            log.warning("scan_report_failed", error=str(e))

    # Step 4: Portfolio risk
    risk_assessment = calculate_portfolio_risk()
    print(f"\nRisk: {risk_assessment.get('risk_score', 0)}/100 "
          f"({risk_assessment.get('risk_level', 'UNKNOWN')})")

    # Step 5: Open positions & exit triggers
    positions = get_open_positions()
    print(f"Open positions: {len(positions)}")
    triggered: list[tuple[dict, dict]] = []
    for pos in positions:
        trigger_result = check_exit_triggers(pos)
        if trigger_result.get("should_exit"):
            triggered.append((pos, trigger_result))
            print(f"  EXIT TRIGGER: {pos['ticker']} — {', '.join(trigger_result.get('triggers', []))}")

    # Step 6: Filter passing signals + pre-trade checks
    passing: list[tuple[dict, dict, dict]] = []
    for sig, score_result in scored:
        if not score_result.get("passed"):
            continue
        ptc = pre_trade_check({**sig, "score": score_result.get("score", 0)}, risk_assessment)
        if ptc.get("approved"):
            passing.append((sig, score_result, ptc))
        else:
            print(f"  Pre-trade DENIED: {sig.get('ticker', '?')} — {', '.join(ptc.get('reasons', []))}")

    # Step 7: Claude entry decisions (if any signals pass)
    if passing:
        print(f"\n{len(passing)} signal(s) passed all checks — calling Claude for entry decision...")
        perf_context = orchestrator.get_performance_context()
        try:
            entry_result = await orchestrator.evaluate_entries(
                passing_signals=passing,
                risk_assessment=risk_assessment,
                positions=positions,
                perf_context=perf_context,
            )
            print(f"\nClaude entry decision:\n{entry_result}")
        except Exception as e:
            print(f"\nEntry evaluation failed: {e}")
    else:
        print(f"\nNo signals passed all checks — no Claude API call needed.")

    # Step 8: Claude exit decisions (if any triggers)
    if triggered:
        print(f"\n{len(triggered)} position(s) triggered exit — calling Claude...")
        try:
            exit_result = await orchestrator.evaluate_exits(
                triggered_positions=triggered,
                risk_assessment=risk_assessment,
            )
            print(f"\nClaude exit decision:\n{exit_result}")
        except Exception as e:
            print(f"\nExit evaluation failed: {e}")

    print("--- End ---\n")


async def run_risk() -> None:
    """Run a risk assessment only."""
    from tools.risk_tools import calculate_portfolio_risk
    from tools.position_tools import get_open_positions

    log = get_logger("run_risk")
    log.info("running_risk_check")

    risk = calculate_portfolio_risk()
    positions = get_open_positions()

    print("\n--- Risk Assessment ---")
    print(f"Risk score: {risk.get('risk_score', 0)}/100 ({risk.get('risk_level', 'UNKNOWN')})")
    print(f"Position count: {risk.get('position_count', 0)}")
    print(f"Risk capacity: {risk.get('risk_capacity_pct', 0):.0%}")
    print(f"Delta exposure: {risk.get('delta_exposure', 0)}")
    print(f"Theta daily: {risk.get('theta_daily_pct', 0):.3%}")
    if risk.get("warnings"):
        print(f"Warnings: {', '.join(risk['warnings'])}")
    print(f"\nOpen positions: {len(positions)}")
    for p in positions:
        print(f"  {p['ticker']} {p.get('action', '?')} ${p.get('strike', 0):.0f} "
              f"DTE={p.get('dte_remaining', 0)} P&L={p.get('pnl_pct', 0):+.1f}%")
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
