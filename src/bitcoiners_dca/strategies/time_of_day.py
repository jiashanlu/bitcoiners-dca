"""
Time-of-day overlay — skip cycles whose hour isn't optimal.

BTC/AED spreads vary across the day. In the UAE-Bitcoiner cycle, 3-6 AM
Dubai-time tends to have the widest spreads (low liquidity), while 9 AM
to 6 PM is tighter. If a user schedules DCA at 9 AM they're already
optimal — but if they let the bot schedule freely, this overlay can shift
buys to the tightest hour.

Two operating modes:

  "skip_if_not_best": if `now` isn't in `preferred_hours`, skip the cycle
  entirely (the scheduler retries on the next configured time).

  "scale_by_spread": multiply the buy by spread_factor, where spread_factor
  is inverse to the median spread at this hour. Buy MORE during tight
  hours, LESS during wide hours, never zero. Less disruptive than skip.

Preferred hours are configured directly (default: 9-18 Asia/Dubai).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bitcoiners_dca.strategies.base import (
    OverlayContext, OverlayResult, StrategyOverlay,
)


@dataclass
class TimeOfDayOverlay(StrategyOverlay):
    name: str = "time_of_day"
    mode: str = "skip_if_not_best"               # skip_if_not_best | scale_by_spread
    preferred_hours: list[int] = field(default_factory=lambda: list(range(9, 19)))
    spread_scale_min: Decimal = Decimal("0.5")   # min multiplier when spread is huge
    spread_scale_max: Decimal = Decimal("1.5")

    def apply(self, ctx: OverlayContext) -> OverlayResult:
        hour = ctx.now.hour
        if self.mode == "skip_if_not_best":
            if hour not in self.preferred_hours:
                return OverlayResult(
                    skip=True,
                    note=f"time-of-day skip (hour={hour} not in {self.preferred_hours})",
                )
            return OverlayResult()
        if self.mode == "scale_by_spread":
            if not ctx.hourly_spread_history:
                return OverlayResult()
            median_now = ctx.hourly_spread_history.get(hour)
            overall = sum(ctx.hourly_spread_history.values()) / Decimal(
                max(len(ctx.hourly_spread_history), 1)
            )
            if median_now is None or overall <= 0:
                return OverlayResult()
            # Tight spread → buy more; wide → buy less
            factor = overall / median_now
            if factor < self.spread_scale_min:
                factor = self.spread_scale_min
            elif factor > self.spread_scale_max:
                factor = self.spread_scale_max
            if factor == Decimal(1):
                return OverlayResult()
            return OverlayResult(
                multiplier=factor,
                note=f"time-of-day scale {factor:.2f}x (hour={hour}, "
                     f"spread={median_now:.3f}% vs avg {overall:.3f}%)",
            )
        return OverlayResult()
