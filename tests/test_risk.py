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


# === Admin auto-pause hook ===

def test_auto_pause_fires_admin_hook_once_on_transition(db):
    """When consecutive failures cross the threshold, the on_auto_pause
    hook fires exactly once — not on every subsequent failed cycle and
    not on a manual pause."""
    calls = []
    rm = RiskManager(db=db, max_consecutive_failures=3)
    rm.on_auto_pause = lambda reason: calls.append(reason)

    # Three failures → auto-pause transitions, hook fires once.
    rm.record_cycle_result(success=False)
    rm.record_cycle_result(success=False)
    rm.record_cycle_result(success=False)
    assert rm.is_paused()
    assert len(calls) == 1
    assert "consecutive failed cycles" in calls[0]

    # A subsequent failed cycle should NOT re-fire (already paused).
    # We have to manually call pause again with the same reason because
    # record_cycle_result keeps incrementing; the dedup is in pause().
    rm.pause("5 consecutive failed cycles (threshold 3)")
    assert len(calls) == 1, "hook re-fired on already-paused pause()"


def test_manual_pause_does_not_fire_admin_hook(db):
    """A pause from the dashboard / CLI (non-auto reason) should not
    fire the admin alert — it's not an incident."""
    calls = []
    rm = RiskManager(db=db)
    rm.on_auto_pause = lambda reason: calls.append(reason)

    rm.pause("Manual pause from dashboard")

    assert rm.is_paused()
    assert calls == [], "hook fired for manual pause"


# === audit 2026-06-10 P1: cap_aed decoupled from the approved base ===

def test_cap_aed_is_min_of_single_buy_and_daily_remainder(db):
    _record_buy(db, "400")  # spent today
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"),
                     max_single_buy_aed=Decimal("800"))
    decision = rm.evaluate(Decimal("100"))
    # Base passes through untouched...
    assert decision.amount_aed == Decimal("100")
    # ...but the ceiling for overlay boosts is min(800, 1000-400) = 600.
    assert decision.cap_aed == Decimal("600")


def test_cap_aed_none_when_no_caps_configured(db):
    rm = RiskManager(db=db)
    decision = rm.evaluate(Decimal("100"))
    assert decision.cap_aed is None


def test_cap_aed_lets_boost_exceed_base_up_to_cap(db):
    """The regression itself: a 2x dip boost on base 100 must be allowed to
    reach 200 when the single-buy cap is 500 — the old code passed the
    approved BASE (100) as the strategy clamp, silently neutering boosts."""
    rm = RiskManager(db=db, max_single_buy_aed=Decimal("500"))
    decision = rm.evaluate(Decimal("100"))
    boosted = decision.amount_aed * 2          # what a 2x overlay produces
    cap = decision.cap_aed
    assert cap == Decimal("500")
    assert boosted < cap                        # boost survives the clamp


# === audit 2026-06-10 P1: daily cap sees stable-funded + partial spends ===

def _record_stable_funded_buy(db: Database, aed_equiv: str, when=None):
    """A BTC/USDT spend from held USDT — pair has no /AED leg; only the
    amount_quote_aed stamp ties it back to the AED budget."""
    when = when or datetime.now(timezone.utc)
    db.record_trade(Order(
        exchange="okx", order_id=f"s-{when.isoformat()}-{aed_equiv}",
        pair="BTC/USDT", side=OrderSide.BUY, type=OrderType.MARKET,
        amount_quote=Decimal(aed_equiv) / Decimal("3.67"),
        amount_base=Decimal("0.0002"),
        price_filled_avg=Decimal("100000"), fee_quote=Decimal("0.1"),
        status=OrderStatus.FILLED, created_at=when, filled_at=when,
        amount_quote_aed=Decimal(aed_equiv),
    ))


def test_daily_cap_counts_stable_funded_cycles(db):
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))
    _record_stable_funded_buy(db, "700")
    assert rm.daily_spend_aed() == Decimal("700")
    decision = rm.evaluate(Decimal("500"))
    # Only 300 of the daily cap remains — the USDT spend counted.
    assert decision.amount_aed == Decimal("300")


def test_daily_cap_counts_partial_fills(db):
    when = datetime.now(timezone.utc)
    db.record_trade(Order(
        exchange="okx", order_id="p-1",
        pair="BTC/AED", side=OrderSide.BUY, type=OrderType.LIMIT,
        amount_quote=Decimal("400"), amount_base=Decimal("0.0005"),
        price_filled_avg=Decimal("350000"),
        status=OrderStatus.PARTIAL, created_at=when,
        amount_quote_aed=Decimal("400"),
    ))
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))
    assert rm.daily_spend_aed() == Decimal("400")


def test_daily_cap_does_not_double_count_two_hop(db):
    """Hop 1 (USDT/AED) carries the AED stamp; hop 2 (BTC/USDT) doesn't.
    Total must be the stamp once, not stamp + hop-2 notional."""
    when = datetime.now(timezone.utc)
    db.record_trade(Order(
        exchange="okx", order_id="h1",
        pair="USDT/AED", side=OrderSide.BUY, type=OrderType.MARKET,
        amount_quote=Decimal("500"), amount_base=Decimal("136"),
        price_filled_avg=Decimal("3.67"),
        status=OrderStatus.FILLED, created_at=when, filled_at=when,
        amount_quote_aed=Decimal("500"),
    ))
    db.record_trade(Order(
        exchange="okx", order_id="h2",
        pair="BTC/USDT", side=OrderSide.BUY, type=OrderType.MARKET,
        amount_quote=Decimal("136"), amount_base=Decimal("0.0013"),
        price_filled_avg=Decimal("100000"),
        status=OrderStatus.FILLED, created_at=when, filled_at=when,
    ))
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))
    assert rm.daily_spend_aed() == Decimal("500")


def test_legacy_aed_rows_without_stamp_still_count(db):
    _record_buy(db, "250")   # no amount_quote_aed — pre-migration row shape
    rm = RiskManager(db=db, max_daily_aed=Decimal("1000"))
    assert rm.daily_spend_aed() == Decimal("250")
