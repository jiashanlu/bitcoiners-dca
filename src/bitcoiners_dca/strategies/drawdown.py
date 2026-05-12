"""
Drawdown-aware sizing — extra buys when BTC is meaningfully below ATH.

Distinct from buy-the-dip (which is short-term: 7d trend). This overlay
looks at how far below all-time-high the current price is. Bear-market
accumulation tool.

Math:
  drawdown_pct = (current - ath) / ath × 100      (negative)
  If drawdown_pct ≤ tier_thresholds[k] → use multiplier_tiers[k]

Default tiers:
  -20% drawdown → 1.5x
  -40% drawdown → 2.5x
  -60% drawdown → 4.0x

The biggest matching tier wins (so -65% gets the 4.0x, not the 1.5x).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bitcoiners_dca.strategies.base import (
    OverlayContext, OverlayResult, StrategyOverlay,
)


@dataclass
class DrawdownTier:
    threshold_pct: Decimal     # e.g. Decimal('-20') for -20%
    multiplier: Decimal        # e.g. Decimal('1.5')


def _default_tiers() -> list[DrawdownTier]:
    return [
        DrawdownTier(Decimal("-20"), Decimal("1.5")),
        DrawdownTier(Decimal("-40"), Decimal("2.5")),
        DrawdownTier(Decimal("-60"), Decimal("4.0")),
    ]


@dataclass
class DrawdownOverlay(StrategyOverlay):
    name: str = "drawdown_aware"
    tiers: list[DrawdownTier] = field(default_factory=_default_tiers)

    def apply(self, ctx: OverlayContext) -> OverlayResult:
        if ctx.current_price_aed is None or ctx.price_ath_aed is None:
            return OverlayResult()
        if ctx.price_ath_aed <= 0:
            return OverlayResult()
        drawdown = (
            (ctx.current_price_aed - ctx.price_ath_aed)
            / ctx.price_ath_aed * Decimal(100)
        )
        # Sort tiers from most-negative threshold first so we pick the deepest match
        sorted_tiers = sorted(self.tiers, key=lambda t: t.threshold_pct)
        for tier in sorted_tiers:
            if drawdown <= tier.threshold_pct:
                return OverlayResult(
                    multiplier=tier.multiplier,
                    note=f"drawdown {drawdown:.1f}% from ATH "
                         f"({tier.multiplier}x at threshold "
                         f"{tier.threshold_pct}%)",
                )
        return OverlayResult()
