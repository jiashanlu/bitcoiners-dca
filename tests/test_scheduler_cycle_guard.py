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
    s._health_fail_streak = {}
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


# ─── health-check alert dampening (consecutive-failure gate) ───────────


def _failing_exchange(name: str):
    ex = MagicMock()
    ex.name = name
    ex.health_check = AsyncMock(side_effect=RuntimeError("BitOasis HTTP 502"))
    return ex


@pytest.mark.asyncio
async def test_single_health_failure_does_not_alert():
    """One transient failure (streak=1) stays silent — no page for a blip."""
    s = _bare_scheduler()
    s.exchanges = [_failing_exchange("bitoasis")]

    await s._run_health_check()

    s.notifier.notify_error.assert_not_awaited()
    assert s._health_fail_streak["bitoasis"] == 1


@pytest.mark.asyncio
async def test_two_consecutive_failures_alert_once():
    """Threshold (2) crossing pages exactly once, not on every check."""
    s = _bare_scheduler()
    s.exchanges = [_failing_exchange("bitoasis")]

    await s._run_health_check()  # streak 1 — silent
    await s._run_health_check()  # streak 2 — alert
    await s._run_health_check()  # streak 3 — already paged, stay quiet

    s.notifier.notify_error.assert_awaited_once()
    assert s._health_fail_streak["bitoasis"] == 3


@pytest.mark.asyncio
async def test_recovery_resets_streak_and_rearming():
    """After an alert + recovery, the gate re-arms: a later blip is silent
    again and only a fresh 2-in-a-row pages a second time."""
    s = _bare_scheduler()
    ex = _failing_exchange("bitoasis")
    s.exchanges = [ex]

    await s._run_health_check()  # 1
    await s._run_health_check()  # 2 → alert #1
    assert s.notifier.notify_error.await_count == 1

    ex.health_check.side_effect = None
    ex.health_check.return_value = True
    await s._run_health_check()  # recovery → streak reset
    assert s._health_fail_streak["bitoasis"] == 0

    ex.health_check.side_effect = RuntimeError("BitOasis HTTP 502")
    await s._run_health_check()  # 1 — silent again (re-armed)
    assert s.notifier.notify_error.await_count == 1
    await s._run_health_check()  # 2 → alert #2
    assert s.notifier.notify_error.await_count == 2


@pytest.mark.asyncio
async def test_one_exchange_failing_does_not_mute_another():
    """Streaks are per-exchange — a flapping BitOasis can't suppress an OKX
    alert and vice versa."""
    s = _bare_scheduler()
    bad = _failing_exchange("bitoasis")
    good = MagicMock()
    good.name = "okx"
    good.health_check = AsyncMock(return_value=True)
    s.exchanges = [bad, good]

    await s._run_health_check()
    await s._run_health_check()

    # bitoasis paged once; okx never failed so its streak stays 0.
    s.notifier.notify_error.assert_awaited_once()
    assert s._health_fail_streak["bitoasis"] == 2
    assert s._health_fail_streak["okx"] == 0


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
