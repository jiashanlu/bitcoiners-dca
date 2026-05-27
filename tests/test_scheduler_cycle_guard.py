"""
Regression: the 5-minute jobs must not rebuild/close exchange clients
while a DCA cycle is in flight.

Root cause (benbois prod, 2026-05-26/27): `_run_arbitrage_check` fired on
its 5-minute tick *during* a DCA cycle that was parked in a maker
`wait_for_fill` poll. It called `_reload_if_changed()`, which rebuilds
dependencies and closes the previous exchange clients — the very clients
the in-flight cycle was still polling with. The next poll then crashed:

  - httpx (BitOasis): "Cannot send a request, as the client has been closed."
  - ccxt/aiodns (OKX/Binance): "'NoneType' object has no attribute 'getaddrinfo'"
  - and surfaced once as "BitOasis network error: /login" (the dying client).

Fix: a `_cycle_in_progress` flag, set synchronously at cycle entry. The
arbitrage / health / funding jobs bail when it is set.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bitcoiners_dca.core.scheduler import DCAScheduler


def _bare_scheduler() -> DCAScheduler:
    """A scheduler with only the attributes the guarded jobs touch —
    skips the real __init__ (RiskManager, apscheduler, MarketDataProvider)."""
    s = DCAScheduler.__new__(DCAScheduler)
    s._cycle_in_progress = False
    s._reload_if_changed = AsyncMock()
    s.exchanges = []
    s.monitor = MagicMock()
    s.monitor.detect = AsyncMock(return_value=[])
    s.notifier = MagicMock()
    s.notifier.notify_error = AsyncMock()
    s.db = MagicMock()
    s.funding_monitor = None
    return s


# ─── arbitrage ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_arbitrage_skips_reload_when_cycle_in_progress():
    s = _bare_scheduler()
    s._cycle_in_progress = True

    await s._run_arbitrage_check()

    s._reload_if_changed.assert_not_awaited()
    s.monitor.detect.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbitrage_reloads_when_no_cycle_running():
    s = _bare_scheduler()
    s._cycle_in_progress = False
    # two exchanges so the <2 short-circuit doesn't hide the reload call
    s.exchanges = [MagicMock(), MagicMock()]

    await s._run_arbitrage_check()

    s._reload_if_changed.assert_awaited_once()
    s.monitor.detect.assert_awaited_once()


# ─── health check ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_skips_when_cycle_in_progress():
    s = _bare_scheduler()
    ex = MagicMock()
    ex.health_check = AsyncMock(return_value=True)
    s.exchanges = [ex]
    s._cycle_in_progress = True

    await s._run_health_check()

    # No client touched, no heartbeat written — fully short-circuited.
    ex.health_check.assert_not_awaited()
    s.db.set_meta.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_runs_when_no_cycle():
    s = _bare_scheduler()
    ex = MagicMock()
    ex.name = "okx"
    ex.health_check = AsyncMock(return_value=True)
    s.exchanges = [ex]
    s._cycle_in_progress = False

    await s._run_health_check()

    ex.health_check.assert_awaited_once()
    s.db.set_meta.assert_called_once()


# ─── funding monitor ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_funding_check_skips_when_cycle_in_progress():
    s = _bare_scheduler()
    # Give it a monitor so the only thing that can short-circuit is the flag.
    s.funding_monitor = MagicMock()
    s._funding_readings = AsyncMock(return_value=[])
    s._cycle_in_progress = True

    await s._run_funding_check()

    s._funding_readings.assert_not_awaited()


# ─── cycle flag lifecycle ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cycle_sets_flag_during_run_and_clears_after():
    s = _bare_scheduler()
    seen: dict[str, bool] = {}

    async def _inner():
        seen["during"] = s._cycle_in_progress

    s._run_dca_cycle_inner = _inner

    assert s._cycle_in_progress is False
    await s._run_dca_cycle()

    assert seen["during"] is True
    assert s._cycle_in_progress is False


@pytest.mark.asyncio
async def test_cycle_clears_flag_even_when_inner_raises():
    s = _bare_scheduler()

    async def _boom():
        raise RuntimeError("cycle exploded")

    s._run_dca_cycle_inner = _boom

    with pytest.raises(RuntimeError):
        await s._run_dca_cycle()

    # The flag must not get stuck — otherwise arb/health stop forever.
    assert s._cycle_in_progress is False
