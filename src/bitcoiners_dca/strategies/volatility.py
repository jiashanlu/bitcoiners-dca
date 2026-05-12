"""
Volatility-weighted DCA — buy LESS when realized volatility is high.

Intuition: when 30d realized vol is "normal" (40-60% annualized for BTC),
DCA full. When vol spikes (>80%), the market is uncertain — smaller buys
preserve cash to deploy later. When vol is unusually low (<30%), buy a
little more (compression often precedes large moves).

Math:
  factor = clamp(1.0 + slope * (target_vol - realized_vol), min_factor, max_factor)

Default slope = 0.02 (per percentage point). target_vol = 50%. So:
  realized_vol = 50%  → factor = 1.0
  realized_vol = 80%  → factor = 1.0 + 0.02 * (50 - 80) = 0.4
  realized_vol = 30%  → factor = 1.4
  clamped to [0.25, 2.0]
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bitcoiners_dca.strategies.base import (
    OverlayContext, OverlayResult, StrategyOverlay,
)


@dataclass
class VolatilityWeightedOverlay(StrategyOverlay):
    name: str = "volatility_weighted"
    target_vol_pct: Decimal = Decimal("50")     # BTC's rough long-run norm
    slope: Decimal = Decimal("0.02")            # sensitivity to vol delta
    min_factor: Decimal = Decimal("0.25")
    max_factor: Decimal = Decimal("2.0")

    def apply(self, ctx: OverlayContext) -> OverlayResult:
        if ctx.realized_vol_30d_pct is None:
            return OverlayResult()
        delta = self.target_vol_pct - ctx.realized_vol_30d_pct
        factor = Decimal(1) + self.slope * delta
        if factor < self.min_factor:
            factor = self.min_factor
        elif factor > self.max_factor:
            factor = self.max_factor
        if factor == Decimal(1):
            return OverlayResult()
        return OverlayResult(
            multiplier=factor,
            note=f"vol-weighted {factor:.2f}x (30d vol {ctx.realized_vol_30d_pct:.0f}% "
                 f"vs target {self.target_vol_pct:.0f}%)",
        )
