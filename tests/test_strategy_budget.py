"""
Unit tests for the budget → per-cycle conversion that powers the
dashboard's "spend AED 1000/month" UX. Anchored to 365-day year math.
"""
from decimal import Decimal

import pytest

from bitcoiners_dca.core.strategy import (
    cycles_per_period,
    derive_per_cycle,
)


# === Passthrough: budget_period="cycle" ===

def test_cycle_period_is_passthrough():
    assert derive_per_cycle(Decimal("500"), "cycle", "weekly") == Decimal("500.00")
    assert derive_per_cycle(Decimal("123.45"), "cycle", "hourly") == Decimal("123.45")


# === Monthly budget, common frequencies ===

def test_monthly_budget_at_weekly_freq():
    # 1000 / month * 12 / 52 weeks = 230.7692... → 230.77
    assert derive_per_cycle(Decimal("1000"), "monthly", "weekly") == Decimal("230.77")


def test_monthly_budget_at_daily_freq():
    # 1000 * 12 / 365 = 32.876... → 32.88
    assert derive_per_cycle(Decimal("1000"), "monthly", "daily") == Decimal("32.88")


def test_monthly_budget_at_monthly_freq_is_passthrough():
    # 1000 * 12 / 12 = 1000.00
    assert derive_per_cycle(Decimal("1000"), "monthly", "monthly") == Decimal("1000.00")


# === Daily budget ===

def test_daily_budget_at_hourly_freq():
    # 100 * 365 / 8760 = 4.1666... → 4.17
    assert derive_per_cycle(Decimal("100"), "daily", "hourly") == Decimal("4.17")


def test_daily_budget_at_weekly_freq():
    # 100 * 365 / 52 = 701.92...
    assert derive_per_cycle(Decimal("100"), "daily", "weekly") == Decimal("701.92")


# === Yearly budget ===

def test_yearly_budget_at_monthly_freq():
    # 12000 / 12 = 1000
    assert derive_per_cycle(Decimal("12000"), "yearly", "monthly") == Decimal("1000.00")


# === Bad input ===

def test_unknown_period_raises():
    with pytest.raises(ValueError, match="unknown budget_period"):
        derive_per_cycle(Decimal("100"), "fortnightly", "daily")


def test_unknown_frequency_raises():
    with pytest.raises(ValueError, match="unknown frequency"):
        derive_per_cycle(Decimal("100"), "monthly", "every-blue-moon")


# === cycles_per_period helper (for the UI preview) ===

def test_cycles_per_period_weekly_at_monthly_freq():
    # 52 weekly cycles / 12 monthly periods = 4.333...
    cpp = cycles_per_period("weekly", "monthly")
    # We expose Decimal — assert it's close to the right value
    assert abs(cpp - Decimal("4.3333")) < Decimal("0.001")


def test_cycles_per_period_cycle_returns_one():
    assert cycles_per_period("daily", "cycle") == Decimal(1)


# === StrategyYamlConfig backfill ===

def test_strategy_yaml_backfills_budget_from_amount_aed():
    """Older config.yaml with only `amount_aed` → budget_amount carries
    that value so the dashboard form doesn't reset to the Pydantic default."""
    from bitcoiners_dca.utils.config import StrategyYamlConfig

    cfg = StrategyYamlConfig(amount_aed=Decimal("750"))
    assert cfg.budget_amount == Decimal("750")
    assert cfg.budget_period == "cycle"


def test_strategy_yaml_respects_explicit_budget():
    """When the YAML has both, budget_amount wins — it's the source of
    truth for what the user typed; amount_aed is the derived per-cycle."""
    from bitcoiners_dca.utils.config import StrategyYamlConfig

    cfg = StrategyYamlConfig(
        amount_aed=Decimal("230.77"),
        budget_amount=Decimal("1000"),
        budget_period="monthly",
        frequency="weekly",
    )
    assert cfg.budget_amount == Decimal("1000")
    assert cfg.budget_period == "monthly"
    assert cfg.amount_aed == Decimal("230.77")
