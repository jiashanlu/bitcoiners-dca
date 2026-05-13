"""
Tests for composable strategy overlays — pure math, no I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.strategies import (
    BuyTheDipOverlay,
    DrawdownOverlay,
    OverlayContext,
    TimeOfDayOverlay,
    VolatilityWeightedOverlay,
)
from bitcoiners_dca.strategies.drawdown import DrawdownTier


def _ctx(**overrides) -> OverlayContext:
    base = dict(
        now=datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
        base_amount_aed=Decimal("500"),
        current_price_aed=Decimal("300000"),
        price_7d_ago_aed=Decimal("310000"),
        price_30d_ago_aed=Decimal("320000"),
        price_ath_aed=Decimal("400000"),
        realized_vol_30d_pct=Decimal("50"),
    )
    base.update(overrides)
    return OverlayContext(**base)


# === BuyTheDipOverlay ===

def test_dip_triggers_when_price_below_threshold():
    ovl = BuyTheDipOverlay(threshold_pct=Decimal("-10"), multiplier=Decimal("2"))
    # current 280k vs 7d ago 320k = -12.5% → triggers
    ctx = _ctx(current_price_aed=Decimal("280000"), price_7d_ago_aed=Decimal("320000"))
    r = ovl.apply(ctx)
    assert r.multiplier == Decimal("2")
    assert "buy-the-dip" in r.note


def test_dip_skips_when_price_change_above_threshold():
    ovl = BuyTheDipOverlay(threshold_pct=Decimal("-10"), multiplier=Decimal("2"))
    ctx = _ctx(current_price_aed=Decimal("305000"), price_7d_ago_aed=Decimal("310000"))
    r = ovl.apply(ctx)
    assert r.multiplier == Decimal(1)
    assert r.note is None


def test_dip_safe_when_history_missing():
    ovl = BuyTheDipOverlay()
    r = ovl.apply(_ctx(price_7d_ago_aed=None))
    assert r.multiplier == Decimal(1)


# === VolatilityWeightedOverlay ===

def test_volatility_reduces_when_vol_above_target():
    ovl = VolatilityWeightedOverlay(
        target_vol_pct=Decimal("50"), slope=Decimal("0.02"),
    )
    # vol 80 → factor = 1 + 0.02*(50-80) = 0.4
    r = ovl.apply(_ctx(realized_vol_30d_pct=Decimal("80")))
    assert r.multiplier == Decimal("0.4")


def test_volatility_increases_when_vol_below_target():
    ovl = VolatilityWeightedOverlay()
    # vol 30 → factor = 1 + 0.02*(50-30) = 1.4
    r = ovl.apply(_ctx(realized_vol_30d_pct=Decimal("30")))
    assert r.multiplier == Decimal("1.4")


def test_volatility_clamps_to_bounds():
    ovl = VolatilityWeightedOverlay(
        min_factor=Decimal("0.25"), max_factor=Decimal("2.0"),
    )
    # Extreme high vol → clamps down
    r_hi = ovl.apply(_ctx(realized_vol_30d_pct=Decimal("200")))
    assert r_hi.multiplier == Decimal("0.25")
    # Extreme low vol → clamps up
    r_lo = ovl.apply(_ctx(realized_vol_30d_pct=Decimal("0")))
    assert r_lo.multiplier == Decimal("2.0")


def test_volatility_no_op_when_data_missing():
    r = VolatilityWeightedOverlay().apply(_ctx(realized_vol_30d_pct=None))
    assert r.multiplier == Decimal(1)
    assert r.note is None


# === TimeOfDayOverlay ===

def test_time_of_day_skip_outside_preferred():
    ovl = TimeOfDayOverlay(mode="skip_if_not_best", preferred_hours=list(range(9, 19)), timezone="UTC")
    # 3 AM Dubai → outside window
    ctx = _ctx(now=datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc))
    r = ovl.apply(ctx)
    assert r.skip is True
    assert "time-of-day" in r.note


def test_time_of_day_allows_preferred_hour():
    ovl = TimeOfDayOverlay(mode="skip_if_not_best", preferred_hours=[9, 10, 11], timezone="UTC")
    ctx = _ctx(now=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc))
    r = ovl.apply(ctx)
    assert r.skip is False


def test_time_of_day_scale_by_spread():
    ovl = TimeOfDayOverlay(mode="scale_by_spread", timezone="UTC")
    history = {h: Decimal("0.05") for h in range(24)}
    history[10] = Decimal("0.025")   # hour 10 has half the avg spread
    ctx = _ctx(
        now=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        hourly_spread_history=history,
    )
    r = ovl.apply(ctx)
    # factor = overall_avg / median_at_hour = 0.05 / 0.025 = 2.0, clamped to 1.5
    assert r.multiplier == Decimal("1.5")


# === DrawdownOverlay ===

def test_drawdown_picks_deepest_matching_tier():
    ovl = DrawdownOverlay(tiers=[
        DrawdownTier(Decimal("-20"), Decimal("1.5")),
        DrawdownTier(Decimal("-40"), Decimal("2.5")),
        DrawdownTier(Decimal("-60"), Decimal("4.0")),
    ])
    # current 100k vs ATH 400k = -75% drawdown
    ctx = _ctx(current_price_aed=Decimal("100000"), price_ath_aed=Decimal("400000"))
    r = ovl.apply(ctx)
    assert r.multiplier == Decimal("4.0")
    assert "drawdown" in r.note


def test_drawdown_intermediate_tier():
    ovl = DrawdownOverlay()
    # current 280k vs ATH 400k = -30% → -20% tier (1.5x), NOT -40% (not reached)
    ctx = _ctx(current_price_aed=Decimal("280000"), price_ath_aed=Decimal("400000"))
    r = ovl.apply(ctx)
    assert r.multiplier == Decimal("1.5")


def test_drawdown_no_op_at_ath():
    ovl = DrawdownOverlay()
    ctx = _ctx(current_price_aed=Decimal("400000"), price_ath_aed=Decimal("400000"))
    r = ovl.apply(ctx)
    assert r.multiplier == Decimal(1)
    assert r.note is None


def test_drawdown_safe_when_history_missing():
    r = DrawdownOverlay().apply(_ctx(price_ath_aed=None))
    assert r.multiplier == Decimal(1)
