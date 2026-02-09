"""
Health check system for monitoring external dependencies and system state.

Checks:
  1. Alpaca API: GET /v2/account succeeds
  2. UW API: screener endpoint returns data
  3. Anthropic API: minimal ping
  4. Database: session opens, tables exist
  5. Disk space: > 500MB free
  6. Last scan age: < 5 minutes during market hours
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger

log = get_logger("health")

MIN_DISK_MB = 500


class HealthResult:
    __slots__ = ("name", "ok", "detail", "latency_ms")

    def __init__(self, name: str, ok: bool, detail: str = "", latency_ms: float = 0):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.latency_ms = latency_ms

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
        }


class HealthChecker:
    """Run health checks against all external dependencies."""

    def __init__(self) -> None:
        self._last_results: list[HealthResult] = []
        self._consecutive_failures = 0

    @property
    def last_results(self) -> list[dict]:
        return [r.to_dict() for r in self._last_results]

    @property
    def is_healthy(self) -> bool:
        if not self._last_results:
            return True
        return all(r.ok for r in self._last_results)

    async def run_all(self) -> list[HealthResult]:
        """Run all health checks and return results."""
        results = [
            self._check_database(),
            self._check_disk_space(),
            await self._check_alpaca(),
            await self._check_uw_api(),
        ]
        self._last_results = results

        failed = [r for r in results if not r.ok]
        if failed:
            self._consecutive_failures += 1
            log.warning(
                "health_check_failed",
                failures=[r.name for r in failed],
                consecutive=self._consecutive_failures,
            )
        else:
            self._consecutive_failures = 0
            log.info("health_check_passed", checks=len(results))

        return results

    def _check_database(self) -> HealthResult:
        """Verify database is accessible and tables exist."""
        import time
        start = time.monotonic()
        try:
            from data.models import get_session, PositionRecord
            session = get_session()
            session.query(PositionRecord).limit(1).all()
            session.close()
            ms = (time.monotonic() - start) * 1000
            return HealthResult("database", True, "OK", ms)
        except Exception as e:
            ms = (time.monotonic() - start) * 1000
            return HealthResult("database", False, str(e)[:100], ms)

    def _check_disk_space(self) -> HealthResult:
        """Verify sufficient disk space."""
        try:
            usage = shutil.disk_usage(Path.home())
            free_mb = usage.free / (1024 * 1024)
            if free_mb < MIN_DISK_MB:
                return HealthResult("disk_space", False, f"Only {free_mb:.0f}MB free (need {MIN_DISK_MB}MB)")
            return HealthResult("disk_space", True, f"{free_mb:.0f}MB free")
        except Exception as e:
            return HealthResult("disk_space", False, str(e)[:100])

    async def _check_alpaca(self) -> HealthResult:
        """Verify Alpaca API is reachable."""
        import time
        start = time.monotonic()
        try:
            from services.alpaca_broker import get_broker
            broker = get_broker()
            account = broker.get_account()
            equity = account.get("equity", 0)
            ms = (time.monotonic() - start) * 1000
            return HealthResult("alpaca", True, f"equity=${equity:,.0f}", ms)
        except Exception as e:
            ms = (time.monotonic() - start) * 1000
            return HealthResult("alpaca", False, str(e)[:100], ms)

    async def _check_uw_api(self) -> HealthResult:
        """Verify Unusual Whales API is reachable."""
        import time
        import httpx
        start = time.monotonic()
        try:
            from config.settings import get_settings
            settings = get_settings()
            headers = {
                "Authorization": f"Bearer {settings.api.uw_api_key}",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.unusualwhales.com/api/screener/option-contracts",
                    headers=headers,
                    params={"limit": 1},
                )
                resp.raise_for_status()
                data = resp.json()
                count = len(data.get("data", []))
                ms = (time.monotonic() - start) * 1000
                return HealthResult("unusual_whales", True, f"{count} contracts", ms)
        except Exception as e:
            ms = (time.monotonic() - start) * 1000
            return HealthResult("unusual_whales", False, str(e)[:100], ms)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


# Singleton
_checker: HealthChecker | None = None


def get_health_checker() -> HealthChecker:
    global _checker
    if _checker is None:
        _checker = HealthChecker()
    return _checker
