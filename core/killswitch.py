"""
File-based kill switch for emergency trading halt.

If the KILLSWITCH file exists in the project root, ALL trading is halted.
The monitor loop checks this at the start of every tick.
"""
from __future__ import annotations

from pathlib import Path

from core.logger import get_logger

log = get_logger("killswitch")

KILLSWITCH_PATH = Path(__file__).resolve().parent.parent / "KILLSWITCH"


def is_killed() -> bool:
    """Check if the kill switch is engaged."""
    return KILLSWITCH_PATH.exists()


def engage(reason: str = "Manual kill switch") -> None:
    """Engage the kill switch by creating the file."""
    KILLSWITCH_PATH.write_text(f"{reason}\n")
    log.critical("killswitch_engaged", reason=reason, path=str(KILLSWITCH_PATH))


def disengage() -> None:
    """Disengage the kill switch by removing the file."""
    if KILLSWITCH_PATH.exists():
        KILLSWITCH_PATH.unlink()
        log.info("killswitch_disengaged", path=str(KILLSWITCH_PATH))
