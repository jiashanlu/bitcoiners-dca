"""
Multi-asset allocation planner tests — pure math.
"""
from __future__ import annotations

from decimal import Decimal

from bitcoiners_dca.strategies.multi_asset import AssetAllocation, plan_cycle


def test_simple_70_30_split():
    legs = plan_cycle(Decimal("1000"), [
        AssetAllocation(asset="BTC", weight=Decimal("0.7")),
        AssetAllocation(asset="ETH", weight=Decimal("0.3")),
    ]).legs
    assert legs == (("BTC", Decimal("700.0")), ("ETH", Decimal("300.0")))


def test_weights_normalize():
    """Weights don't need to sum to 1.0 — they're normalized."""
    legs = plan_cycle(Decimal("1000"), [
        AssetAllocation(asset="BTC", weight=Decimal("7")),
        AssetAllocation(asset="ETH", weight=Decimal("3")),
    ]).legs
    assert legs == (("BTC", Decimal("700")), ("ETH", Decimal("300")))


def test_below_minimum_redistributes_to_others():
    """ETH's nominal share is below its min_buy → redistributes to BTC."""
    legs = plan_cycle(Decimal("1000"), [
        AssetAllocation(asset="BTC", weight=Decimal("0.95")),
        AssetAllocation(asset="ETH", weight=Decimal("0.05"), min_buy_aed=Decimal("100")),
    ]).legs
    # ETH gets 50 AED nominally, below 100 → redistributed
    assert len(legs) == 1
    assert legs[0][0] == "BTC"
    assert legs[0][1] == Decimal("1000")


def test_three_asset_with_one_dropped():
    legs = plan_cycle(Decimal("1000"), [
        AssetAllocation(asset="BTC", weight=Decimal("0.6")),
        AssetAllocation(asset="ETH", weight=Decimal("0.3")),
        AssetAllocation(asset="SOL", weight=Decimal("0.1"), min_buy_aed=Decimal("200")),
    ]).legs
    # SOL nominal = 100 AED, below 200 min → 100 AED redistributed.
    # BTC weight=0.6, ETH weight=0.3, total remaining=0.9
    # BTC bonus = 100 × 0.6/0.9 = 66.67; ETH = 100 × 0.3/0.9 = 33.33
    assert len(legs) == 2
    assets = {a: amt for a, amt in legs}
    assert "BTC" in assets and "ETH" in assets and "SOL" not in assets
    # Tolerance for Decimal rounding
    assert abs(assets["BTC"] - Decimal("666.6666666666666666666666667")) < Decimal("0.0001")
    assert abs(assets["ETH"] - Decimal("333.3333333333333333333333333")) < Decimal("0.0001")


def test_empty_allocations_returns_empty_plan():
    assert plan_cycle(Decimal("1000"), []).legs == ()


def test_all_legs_below_minimum_returns_empty():
    legs = plan_cycle(Decimal("100"), [
        AssetAllocation(asset="BTC", weight=Decimal("0.5"), min_buy_aed=Decimal("200")),
        AssetAllocation(asset="ETH", weight=Decimal("0.5"), min_buy_aed=Decimal("200")),
    ]).legs
    assert legs == ()
