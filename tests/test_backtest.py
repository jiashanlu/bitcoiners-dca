"""
Backtest engine tests — verify cadence, dip overlay triggers, and totals
against synthetic price histories.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.backtest import (
    BacktestConfig,
    naive_baseline,
    run_backtest,
)
from bitcoiners_dca.core.historical_prices import PricePoint


def _series(prices: list[tuple[str, str]]) -> list[PricePoint]:
    """[(YYYY-MM-DD, '350000'), ...] → PricePoint list."""
    return [
        PricePoint(
            timestamp=datetime.fromisoformat(d).replace(tzinfo=timezone.utc),
            price=Decimal(p),
        )
        for d, p in prices
    ]


# === Cadence ===

def test_daily_cadence_fires_every_day():
    prices = _series([(f"2026-01-0{i}", "100000") for i in range(1, 6)])  # 5 days
    cfg = BacktestConfig(base_amount_aed=Decimal("100"), frequency="daily")

    result = run_backtest(cfg, prices)

    assert result.cycle_count == 5
    assert result.total_aed_spent == Decimal("500")


def test_weekly_cadence_fires_on_configured_day():
    # 2026-01-05 is a Monday. 12, 19, 26 are also Mondays.
    days = []
    cur = date(2026, 1, 1)
    while cur <= date(2026, 1, 28):
        days.append(cur)
        cur += timedelta(days=1)
    prices = _series([(d.isoformat(), "100000") for d in days])

    cfg = BacktestConfig(
        base_amount_aed=Decimal("100"), frequency="weekly", day_of_week=0
    )
    result = run_backtest(cfg, prices)

    fired_days = [c.day for c in result.cycles]
    assert fired_days == [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19), date(2026, 1, 26)]


def test_monthly_cadence_fires_on_first_only():
    prices = _series([
        ("2026-01-01", "100000"),
        ("2026-01-15", "110000"),
        ("2026-02-01", "120000"),
        ("2026-02-20", "130000"),
        ("2026-03-01", "140000"),
    ])
    cfg = BacktestConfig(base_amount_aed=Decimal("500"), frequency="monthly")

    result = run_backtest(cfg, prices)

    assert [c.day for c in result.cycles] == [
        date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
    ]


# === Buy math ===

def test_buy_math_applies_taker_fee():
    """At 100,000 AED/BTC with 0.5% fee, a 1,000 AED buy gets 1000 / (100000 * 1.005)."""
    prices = _series([("2026-01-05", "100000")])
    cfg = BacktestConfig(
        base_amount_aed=Decimal("1000"),
        frequency="weekly", day_of_week=0,
        taker_fee_pct=Decimal("0.005"),
    )

    result = run_backtest(cfg, prices)

    assert len(result.cycles) == 1
    c = result.cycles[0]
    expected_btc = Decimal("1000") / (Decimal("100000") * Decimal("1.005"))
    assert c.btc_bought == expected_btc


# === Dip overlay ===

def test_dip_overlay_triggers_on_drop():
    """Price drops from 100k to 88k (–12%) — overlay triggers, doubles the buy."""
    days = []
    for i in range(8):
        d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        # day 0..6 = flat 100k; day 7 (Mon) = 88k (–12% in 7 days)
        price = "100000" if i < 7 else "88000"
        days.append((d, price))
    prices = _series(days)

    cfg = BacktestConfig(
        base_amount_aed=Decimal("500"),
        frequency="weekly", day_of_week=0,  # day 7 from 2026-01-01 = 2026-01-08 = Thursday actually
        dip_overlay_enabled=True,
        dip_threshold_pct=Decimal("-10"),
        dip_multiplier=Decimal("2.0"),
        dip_lookback_days=7,
    )

    # 2026-01-01 = Thursday, so day_of_week=0 (Mon) fires on 2026-01-05 and 2026-01-12.
    # We want 2026-01-12 (offset 11 from jan-01) to see an 88k price — a 12% drop vs
    # the 7-days-prior reference of 100k.
    days2 = []
    for i in range(13):
        d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        days2.append((d, "100000" if i < 11 else "88000"))
    prices2 = _series(days2)

    result = run_backtest(cfg, prices2)

    fired = {c.day: c for c in result.cycles}
    # 2026-01-05 fires at flat price (no overlay)
    # 2026-01-12 fires at 88k (overlay triggers)
    assert date(2026, 1, 5) in fired
    assert fired[date(2026, 1, 5)].overlay_applied is False
    assert fired[date(2026, 1, 5)].aed_spent == Decimal("500")
    assert date(2026, 1, 12) in fired
    assert fired[date(2026, 1, 12)].overlay_applied is True
    assert fired[date(2026, 1, 12)].aed_spent == Decimal("1000")


def test_dip_overlay_does_not_trigger_above_threshold():
    """Price drop is only 5% — overlay should NOT fire."""
    days = []
    for i in range(13):
        d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        days.append((d, "100000" if i < 12 else "95000"))
    prices = _series(days)

    cfg = BacktestConfig(
        base_amount_aed=Decimal("500"),
        frequency="weekly", day_of_week=0,
        dip_overlay_enabled=True,
        dip_threshold_pct=Decimal("-10"),
    )

    result = run_backtest(cfg, prices)
    for c in result.cycles:
        assert c.overlay_applied is False
        assert c.aed_spent == Decimal("500")


# === Naive baseline ===

def test_naive_baseline_disables_overlay():
    days = [(f"2026-01-{i:02d}", "100000") for i in range(1, 15)]
    prices = _series(days)
    cfg = BacktestConfig(
        base_amount_aed=Decimal("500"),
        frequency="weekly", day_of_week=0,
        dip_overlay_enabled=True, dip_multiplier=Decimal("5.0"),
    )
    baseline = naive_baseline(cfg, prices)
    # Same cadence, but each cycle should be at base amount only
    assert all(c.aed_spent == Decimal("500") for c in baseline.cycles)
    assert all(c.overlay_applied is False for c in baseline.cycles)


# === Empty / edge cases ===

def test_empty_history_returns_empty_result():
    result = run_backtest(
        BacktestConfig(base_amount_aed=Decimal("100"), frequency="daily"), []
    )
    assert result.cycle_count == 0
    assert result.total_aed_spent == Decimal(0)
