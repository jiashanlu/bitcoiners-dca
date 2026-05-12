"""
Strategy overlays — pluggable amount modifiers applied to the base DCA buy.

The base strategy says "buy AED N every cycle". Overlays mutate N based on
market state:

  - buy_the_dip  : multiply N when BTC has dropped > threshold in lookback
  - volatility_weighted : reduce N when realized volatility is high
  - time_of_day  : skip cycles that aren't at the cheapest hour of day
  - drawdown     : multiply N when BTC is meaningfully off ATH

Overlays are composable. Each overlay returns an `OverlayResult` describing
the modification + a human-readable note for the audit log. The strategy
applies overlays in config-defined order; their effects multiply.

See `docs/STRATEGIES.md` for deep dives on each.
"""
from bitcoiners_dca.strategies.base import (
    OverlayContext,
    OverlayResult,
    StrategyOverlay,
)
from bitcoiners_dca.strategies.dip import BuyTheDipOverlay
from bitcoiners_dca.strategies.drawdown import DrawdownOverlay
from bitcoiners_dca.strategies.time_of_day import TimeOfDayOverlay
from bitcoiners_dca.strategies.volatility import VolatilityWeightedOverlay

__all__ = [
    "OverlayContext",
    "OverlayResult",
    "StrategyOverlay",
    "BuyTheDipOverlay",
    "DrawdownOverlay",
    "TimeOfDayOverlay",
    "VolatilityWeightedOverlay",
]
