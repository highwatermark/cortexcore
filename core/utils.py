"""
Shared utility functions.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import NamedTuple

import pytz

# Eastern Time — the canonical timezone for US equity/options trading days.
ET = pytz.timezone("America/New_York")


def trading_today_et() -> str:
    """Return today's date string (YYYY-MM-DD) in Eastern Time.

    Use this for any day-boundary logic (daily counters, loss limits, calendar
    checks).  UTC midnight != ET midnight, so using UTC causes counters to
    reset at 7 PM ET (winter) / 8 PM ET (summer) instead of midnight.
    """
    return datetime.now(ET).strftime("%Y-%m-%d")


def trading_now_et() -> datetime:
    """Return current datetime in Eastern Time (timezone-aware)."""
    return datetime.now(ET)


class OccSymbol(NamedTuple):
    """Parsed OCC option symbol components."""
    ticker: str
    expiration: str  # YYYY-MM-DD
    option_type: str  # CALL or PUT
    strike: float
    raw: str  # original symbol


# OCC format: TICKER + YYMMDD + C/P + 00000000 (strike * 1000, zero-padded to 8 digits)
_OCC_PATTERN = re.compile(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$')


def parse_occ_symbol(symbol: str) -> OccSymbol | None:
    """Parse an OCC option symbol like AAPL250321C00175000.

    Returns OccSymbol with ticker, expiration (YYYY-MM-DD), option_type (CALL/PUT),
    strike price, and the raw symbol. Returns None if parsing fails.
    """
    symbol = symbol.strip().upper()
    m = _OCC_PATTERN.match(symbol)
    if not m:
        return None

    ticker = m.group(1)
    date_str = m.group(2)  # YYMMDD
    cp = m.group(3)
    strike_raw = m.group(4)

    try:
        # Parse date: YYMMDD -> YYYY-MM-DD
        dt = datetime.strptime(date_str, "%y%m%d")
        expiration = dt.strftime("%Y-%m-%d")
    except ValueError:
        return None

    option_type = "CALL" if cp == "C" else "PUT"
    strike = int(strike_raw) / 1000.0

    return OccSymbol(
        ticker=ticker,
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        raw=symbol,
    )


def calc_dte(expiration: str) -> int:
    """Calculate days to expiration from a YYYY-MM-DD date string.

    Uses ET so that DTE doesn't flip a day early near UTC midnight.
    """
    if not expiration:
        return 0
    try:
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        today = datetime.now(ET).date()
        return max(0, (exp_date - today).days)
    except ValueError:
        return 0
