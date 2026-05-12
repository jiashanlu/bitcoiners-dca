"""
Buy-the-dip overlay — multiply buy size when BTC dropped > threshold in lookback.

Math: with `threshold_pct=-10, multiplier=2`, when (current / lookback - 1)*100
crosses -10%, the buy doubles. Below the threshold the cycle uses base amount.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bitcoiners_dca.strategies.base import (
    OverlayContext, OverlayResult, StrategyOverlay,
)


@dataclass
class BuyTheDipOverlay(StrategyOverlay):
    name: str = "buy_the_dip"
    threshold_pct: Decimal = Decimal("-10")
    multiplier: Decimal = Decimal("2.0")
    lookback_days: int = 7

    def apply(self, ctx: OverlayContext) -> OverlayResult:
        if ctx.current_price_aed is None or ctx.price_7d_ago_aed is None:
            return OverlayResult()
        if ctx.price_7d_ago_aed <= 0:
            return OverlayResult()
        pct_change = (
            (ctx.current_price_aed - ctx.price_7d_ago_aed)
            / ctx.price_7d_ago_aed * Decimal(100)
        )
        if pct_change <= self.threshold_pct:
            return OverlayResult(
                multiplier=self.multiplier,
                note=f"buy-the-dip {self.multiplier}x (price down "
                     f"{pct_change:.1f}% in {self.lookback_days}d)",
            )
        return OverlayResult()
