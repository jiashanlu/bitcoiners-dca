"""
Multi-asset DCA — Business-tier feature. Allocate a single cycle budget
across multiple base assets (BTC + ETH + SOL + …) according to user
weights, then run separate routing decisions for each leg.

Why it exists: some users want a mostly-BTC stack with a small index-like
exposure to other large caps. Or they want to DCA a "Lindy basket" (BTC
+ ETH only). Manually splitting the AED across two or three buys per
cycle is annoying — this overlay automates it.

DESIGN STATUS: scaffold only — the model + config wiring lives here but
the strategy/scheduler integration ships in v0.7. Free + Pro tiers ignore
this entirely; Business tier sees it active.

The math is straightforward:
    for asset, weight in allocations:
        leg_amount = total_cycle_amount × weight / sum(weights)
        run normal routing + execution for that asset

Constraint: every asset must be routable from the user's quote currency
on at least one enabled exchange. The router transparently picks the
best path per leg (including multi-hop where available).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AssetAllocation:
    """One leg of a multi-asset cycle.

    weight is a relative number — they don't have to sum to 1.0; we
    normalize. min_buy_aed prevents micro-buys when a leg's weighted
    share is below the exchange's minimum order size.
    """
    asset: str               # e.g. "BTC", "ETH", "SOL"
    weight: Decimal          # relative weight, e.g. 0.7 for 70% of cycle
    min_buy_aed: Decimal = Decimal("50")


@dataclass(frozen=True)
class MultiAssetPlan:
    """Concrete per-asset amounts for one cycle.

    Built by `plan_cycle` below. The scheduler walks `legs` and runs the
    normal routing + execution stack for each one.
    """
    legs: tuple[tuple[str, Decimal], ...]   # (asset, amount_aed)


def plan_cycle(
    total_amount_aed: Decimal,
    allocations: list[AssetAllocation],
) -> MultiAssetPlan:
    """Split `total_amount_aed` across `allocations` proportional to weights.

    Skips legs whose share would fall below their `min_buy_aed`. The skipped
    leg's share is redistributed across the remaining legs in weight-order.
    """
    if not allocations:
        return MultiAssetPlan(legs=())

    # Filter zero-or-negative weights
    valid = [a for a in allocations if a.weight > 0]
    if not valid:
        return MultiAssetPlan(legs=())

    total_weight = sum((a.weight for a in valid), Decimal(0))
    # First pass: nominal split
    nominal = [
        (a, (total_amount_aed * a.weight / total_weight))
        for a in valid
    ]
    # Drop legs below their minimum, gather the leftover for redistribution
    too_small = [(a, amt) for a, amt in nominal if amt < a.min_buy_aed]
    big_enough = [(a, amt) for a, amt in nominal if amt >= a.min_buy_aed]

    if not big_enough:
        # Everything's too small — buy nothing this cycle
        return MultiAssetPlan(legs=())

    leftover = sum((amt for _, amt in too_small), Decimal(0))
    if leftover > 0:
        # Redistribute proportional to remaining weights
        remaining_weight = sum((a.weight for a, _ in big_enough), Decimal(0))
        big_enough = [
            (a, amt + leftover * a.weight / remaining_weight)
            for a, amt in big_enough
        ]

    return MultiAssetPlan(legs=tuple((a.asset, amt) for a, amt in big_enough))
