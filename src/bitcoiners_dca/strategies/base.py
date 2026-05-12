"""
Strategy-overlay base types — what every overlay returns + the context it sees.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class OverlayContext:
    """Everything an overlay can read to make its decision.

    Kept minimal so overlays don't acquire hidden dependencies on the rest
    of the codebase. If an overlay needs new data, add it here so all
    overlays see a consistent picture of the cycle.
    """
    now: datetime                                    # cycle wall-clock
    base_amount_aed: Decimal                         # the user's configured amount
    current_price_aed: Optional[Decimal] = None      # cheapest live ask across exchanges
    price_7d_ago_aed: Optional[Decimal] = None       # historical, supplied by scheduler
    price_30d_ago_aed: Optional[Decimal] = None
    price_ath_aed: Optional[Decimal] = None
    realized_vol_30d_pct: Optional[Decimal] = None   # annualized %
    hourly_spread_history: Optional[dict[int, Decimal]] = None  # hour-of-day → median spread%


@dataclass
class OverlayResult:
    """An overlay's verdict.

    - `multiplier == 1.0` means "no change". Multipliers compound across
      multiple overlays.
    - `skip == True` means "skip this cycle entirely" (e.g. time-of-day
      says this isn't the right hour). The strategy short-circuits on the
      first skip.
    - `note` goes into the cycle's audit log + Telegram notification.
    """
    multiplier: Decimal = field(default_factory=lambda: Decimal(1))
    skip: bool = False
    note: Optional[str] = None


class StrategyOverlay(ABC):
    """Subclass + override `apply()`. Constructor takes the overlay's own config."""

    name: str = "overlay"

    @abstractmethod
    def apply(self, ctx: OverlayContext) -> OverlayResult:
        """Inspect context, return a result. Pure function — no I/O."""
