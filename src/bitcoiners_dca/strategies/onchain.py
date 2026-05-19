"""
On-chain smart-trigger overlay — multiplies the cycle buy by a band-based
factor derived from a BRK metric (MVRV, MVRV-Z, SOPR, Pi-Cycle).

The overlay itself is pure-sync (like the others). The strategy fetches
the metric value before the overlay loop and supplies it via
`OverlayContext.onchain_signals`. Keeping the overlay pure means it stays
trivial to unit-test and the same fetch can be shared across overlays.

Multiplier clamping: the strategy already clamps the *total* per-cycle
amount via overlay multipliers compounding. To stop a misconfigured band
from skipping or 10x-ing a cycle, we additionally clamp the overlay's
own output to [0.5, 2.0]. DCA must keep flowing — never return skip=True.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bitcoiners_dca.strategies.base import (
    OverlayContext, OverlayResult, StrategyOverlay,
)


_MIN_MULTIPLIER = Decimal("0.5")
_MAX_MULTIPLIER = Decimal("2.0")


@dataclass
class OnchainSmartTriggerOverlay(StrategyOverlay):
    name: str = "onchain_smart_trigger"
    metric: str = "mvrv_z"
    boost_below: Decimal = Decimal("-1.0")
    boost_multiplier: Decimal = Decimal("1.5")
    dampen_above: Decimal = Decimal("2.0")
    dampen_multiplier: Decimal = Decimal("0.5")

    def apply(self, ctx: OverlayContext) -> OverlayResult:
        signals = ctx.onchain_signals or {}
        value = signals.get(self.metric)
        if value is None:
            # Signal unavailable (network error, not enabled, etc.) — no-op.
            return OverlayResult(note=f"{self.metric}: signal unavailable, no boost")

        if value <= self.boost_below:
            mult = max(_MIN_MULTIPLIER, min(_MAX_MULTIPLIER, self.boost_multiplier))
            return OverlayResult(
                multiplier=mult,
                note=f"{self.metric}={value:.3f} ≤ {self.boost_below} → boost {mult}x",
            )
        if value >= self.dampen_above:
            mult = max(_MIN_MULTIPLIER, min(_MAX_MULTIPLIER, self.dampen_multiplier))
            return OverlayResult(
                multiplier=mult,
                note=f"{self.metric}={value:.3f} ≥ {self.dampen_above} → dampen {mult}x",
            )
        return OverlayResult(note=f"{self.metric}={value:.3f} in neutral band")
