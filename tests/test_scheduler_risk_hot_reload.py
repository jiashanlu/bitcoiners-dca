"""
Regression: risk CAPS must hot-reload on a config change — without a restart.

Bug (benbois prod): raising the single-buy cap in the dashboard never took
effect until a container restart. `_reload_if_changed` did
`self.risk = fresh.get("risk", self.risk)`, but the rebuild factory returned
no "risk" key, so it always fell back to the STARTUP RiskManager — whose
max_single_buy_aed was still the boot-time value. Every cycle then clamped the
raised amount back down to the stale cap ("risk-approved amount=AED 50").

Fix: `_reload_if_changed` refreshes the caps IN PLACE on the existing
RiskManager. All risk state (pause flag, consecutive-failure count, daily
spend) lives in the DB; only the caps + the on_auto_pause hook live on the
instance — so we mutate, never swap (a swap would silently drop the hook).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from bitcoiners_dca.core.scheduler import DCAScheduler


def _config(single_buy_cap: str, *, frequency: str = "daily", timezone: str = "Asia/Dubai"):
    """Minimal config double carrying only the fields _reload_if_changed reads."""
    return SimpleNamespace(
        risk=SimpleNamespace(
            max_daily_aed=Decimal("500"),
            max_single_buy_aed=Decimal(single_buy_cap),
            max_consecutive_failures=3,
        ),
        strategy=SimpleNamespace(
            timezone=timezone,
            frequency=frequency,
            every_n_hours=1,
            day_of_week=None,
            time="09:00",
            amount_aed=Decimal("82.19"),
        ),
    )


def _reload_only_scheduler(old_cfg, risk) -> DCAScheduler:
    """Bare scheduler exercising only _reload_if_changed — no apscheduler."""
    s = DCAScheduler.__new__(DCAScheduler)
    s.config = old_cfg
    s.exchanges = []  # empty → skips the replaced-client close loop
    s.risk = risk
    return s


@pytest.mark.asyncio
async def test_raised_single_buy_cap_hot_reloads():
    """Operator raises the cap 50 → 100; next reload must apply it."""
    old_cfg = _config("50")
    hook = lambda reason: None  # noqa: E731 — sentinel to prove the instance survives
    risk = SimpleNamespace(
        max_daily_aed=Decimal("500"),
        max_single_buy_aed=Decimal("50"),  # stale boot-time cap
        max_consecutive_failures=3,
        timezone_str="Asia/Dubai",
        on_auto_pause=hook,
    )
    s = _reload_only_scheduler(old_cfg, risk)

    fresh_cfg = _config("100")  # cron fields unchanged → no reschedule path
    s._rebuild_dependencies = lambda: {
        "config": fresh_cfg,
        "exchanges": [],
        "strategy": fresh_cfg.strategy,
        "router": object(),
        "monitor": object(),
    }

    await s._reload_if_changed()

    # caps now reflect the fresh config
    assert s.risk.max_single_buy_aed == Decimal("100")
    assert s.config.risk.max_single_buy_aed == Decimal("100")


@pytest.mark.asyncio
async def test_reload_mutates_in_place_and_keeps_auto_pause_hook():
    """The RiskManager instance is reused (not swapped) so on_auto_pause and
    all DB-backed state survive a hot-reload."""
    old_cfg = _config("50")
    hook = lambda reason: None  # noqa: E731
    risk = SimpleNamespace(
        max_daily_aed=Decimal("200"),
        max_single_buy_aed=Decimal("50"),
        max_consecutive_failures=3,
        timezone_str="Asia/Dubai",
        on_auto_pause=hook,
    )
    s = _reload_only_scheduler(old_cfg, risk)

    fresh_cfg = _config("100")
    fresh_cfg.risk.max_daily_aed = Decimal("750")
    fresh_cfg.risk.max_consecutive_failures = 5
    s._rebuild_dependencies = lambda: {
        "config": fresh_cfg,
        "exchanges": [],
        "strategy": fresh_cfg.strategy,
        "router": object(),
        "monitor": object(),
    }

    await s._reload_if_changed()

    assert s.risk is risk                      # same object, not replaced
    assert s.risk.on_auto_pause is hook        # hook preserved
    assert s.risk.max_daily_aed == Decimal("750")
    assert s.risk.max_consecutive_failures == 5
