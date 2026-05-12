"""
RiskManager tests — verify the three protections (pause, daily cap, single-buy
cap) and the auto-pause after consecutive failures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.models import Order, OrderSide, OrderStatus, OrderType
from bitcoiners_dca.core.risk import RiskManager
from bitcoiners_dca.persistence.db import Database


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "risk.db"))
    yield db
    db.close()


def _record_buy(db: Database, amount: str = "500", when: datetime = None):
    when = when or datetime.now(timezone.utc)
    db.record_trade(Order(
        exchange="okx", order_id=f"r-{when.isoformat()}-{amount}",
        pair="BTC/AED", side=OrderSide.BUY, type=OrderType.MARKET,
        amount_quote=Decimal(amount), amount_base=Decimal("0.001"),
        price_filled_avg=Decimal("350000"), fee_quote=Decimal("0.5"),
        status=OrderStatus.FILLED, created_at=when, filled_at=when,
    ))


# === Pause state ===

def test_paused_blocks_everything(db):
    rm = RiskManager(db=db, max_daily_aed=None)
    rm.pause("manual test")

    decision = rm.evaluate(Decimal("500"))

    assert not decision.allow
    assert "paused" in decision.reasons[0]
    assert decision.amount_aed == Decimal("0")


def test_resume_clears_state(db):
    rm = RiskManager(db=db)
    rm.pause("test")
    assert rm.is_paused()
    rm.resume()
    assert not rm.is_paused()
    assert rm.consecutive_failures() == 0


# === Single-buy cap ===

def test_single_buy_cap_clamps(db):
    rm = RiskManager(db=db, max_single_buy_aed=Decimal("1000"))

    decision = rm.evaluate(Decimal("1500"))

    assert decision.allow
    assert decision.amount_aed == Decimal("1000")
    assert any("single-buy cap" in r for r in decision.reasons)


def test_single_buy_cap_passes_through_when_under(db):
    rm = RiskManager(db=db, max_single_buy_aed=Decimal("1000"))

    decision = rm.evaluate(Decimal("500"))

    assert decision.allow
    assert decision.amount_aed == Decimal("500")
    assert not any("clamp" in r for r in decision.reasons)


# === Daily cap ===

def test_daily_cap_clamps_when_partial_budget_remains(db):
    _record_buy(db, "700")  # already spent 700 today
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))

    decision = rm.evaluate(Decimal("500"))

    assert decision.allow
    # Remaining budget = 1000 - 700 = 300, intended 500 → clamp to 300
    assert decision.amount_aed == Decimal("300")
    assert any("daily-cap" in r for r in decision.reasons)


def test_daily_cap_blocks_when_exhausted(db):
    _record_buy(db, "1000")  # already at cap
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))

    decision = rm.evaluate(Decimal("500"))

    assert not decision.allow
    assert any("daily cap reached" in r for r in decision.reasons)


def test_daily_cap_ignores_yesterday(db):
    """Spend from a previous UTC day must not count against today's cap."""
    yesterday = datetime.now(timezone.utc).replace(
        year=2020, month=1, day=1, hour=12,
    )
    _record_buy(db, "1000", when=yesterday)
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))

    decision = rm.evaluate(Decimal("500"))

    assert decision.allow
    assert decision.amount_aed == Decimal("500")


# === Auto-pause after consecutive failures ===

def test_consecutive_failures_trigger_pause(db):
    rm = RiskManager(db=db, max_consecutive_failures=3)

    rm.record_cycle_result(success=False)
    rm.record_cycle_result(success=False)
    assert not rm.is_paused()
    assert rm.consecutive_failures() == 2

    rm.record_cycle_result(success=False)
    assert rm.is_paused()
    assert "consecutive" in (rm.paused_reason() or "")


def test_success_resets_failure_counter(db):
    rm = RiskManager(db=db, max_consecutive_failures=3)

    rm.record_cycle_result(success=False)
    rm.record_cycle_result(success=False)
    rm.record_cycle_result(success=True)

    assert rm.consecutive_failures() == 0
    assert not rm.is_paused()


# === No caps configured ===

def test_no_caps_allows_full_amount(db):
    rm = RiskManager(db=db)  # all None

    decision = rm.evaluate(Decimal("9999"))

    assert decision.allow
    assert decision.amount_aed == Decimal("9999")
    assert decision.reasons == []
