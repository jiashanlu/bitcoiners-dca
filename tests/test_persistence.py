"""
Persistence layer tests — verify round-trip storage of trades, withdrawals,
arbitrage, and cycles. Uses an in-memory-ish SQLite (tmp file).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bitcoiners_dca.core.models import (
    ArbitrageOpportunity, Order, OrderSide, OrderStatus, OrderType,
    Withdrawal, WithdrawalStatus,
)
from bitcoiners_dca.persistence.db import Database


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    yield db
    db.close()


def _make_order(
    exchange="okx",
    order_id="o-1",
    amount_quote="500",
    amount_base="0.0014",
    price="357142",
    fee="0.75",
    status=OrderStatus.FILLED,
) -> Order:
    return Order(
        exchange=exchange,
        order_id=order_id,
        pair="BTC/AED",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        amount_quote=Decimal(amount_quote),
        amount_base=Decimal(amount_base),
        price_filled_avg=Decimal(price),
        fee_quote=Decimal(fee),
        status=status,
        created_at=datetime.now(timezone.utc),
        filled_at=datetime.now(timezone.utc),
    )


# === Trades ===

def test_record_and_aggregate_trades(tmp_db):
    tmp_db.record_trade(_make_order(order_id="o-1", amount_quote="500", amount_base="0.0014"))
    tmp_db.record_trade(_make_order(order_id="o-2", amount_quote="500", amount_base="0.0013"))

    assert tmp_db.total_aed_spent() == Decimal("1000")
    assert tmp_db.total_btc_bought() == Decimal("0.0027")


def test_trade_dedup_via_unique_constraint(tmp_db):
    """INSERT OR REPLACE keeps idempotency on (exchange, order_id)."""
    tmp_db.record_trade(_make_order(order_id="dup", amount_quote="500", amount_base="0.001"))
    tmp_db.record_trade(_make_order(order_id="dup", amount_quote="500", amount_base="0.001"))

    # Sum should reflect only one trade, not two
    assert tmp_db.total_aed_spent() == Decimal("500")


def test_trade_status_filter(tmp_db):
    """Non-filled trades shouldn't count in totals."""
    tmp_db.record_trade(_make_order(order_id="filled", status=OrderStatus.FILLED))
    tmp_db.record_trade(_make_order(
        order_id="cancelled", status=OrderStatus.CANCELLED,
        amount_quote="999", amount_base="0.1"
    ))

    assert tmp_db.total_aed_spent() == Decimal("500")  # only the filled one


# === Withdrawals ===

def test_record_withdrawal_roundtrip(tmp_db):
    w = Withdrawal(
        exchange="okx",
        withdrawal_id="w-1",
        asset="BTC",
        amount=Decimal("0.01"),
        address="bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        fee=Decimal("0.0002"),
        status=WithdrawalStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    tmp_db.record_withdrawal(w)

    # The table is queryable directly
    cur = tmp_db._conn.execute("SELECT * FROM withdrawals WHERE withdrawal_id = 'w-1'")
    row = cur.fetchone()
    assert row["exchange"] == "okx"
    assert Decimal(row["amount"]) == Decimal("0.01")
    assert row["status"] == "pending"


# === Arbitrage log ===

def test_record_arbitrage_increments(tmp_db):
    opp = ArbitrageOpportunity(
        timestamp=datetime.now(timezone.utc),
        pair="BTC/AED",
        cheap_exchange="okx",
        cheap_ask=Decimal("350000"),
        expensive_exchange="bitoasis",
        expensive_bid=Decimal("355000"),
        spread_pct=Decimal("1.43"),
        net_profit_pct_after_fees=Decimal("0.8"),
    )
    tmp_db.record_arbitrage(opp, alerted=True)
    tmp_db.record_arbitrage(opp, alerted=False)

    cur = tmp_db._conn.execute("SELECT COUNT(*) AS n FROM arbitrage_log")
    assert cur.fetchone()["n"] == 2


# === Meta ===

def test_meta_get_set_update(tmp_db):
    assert tmp_db.get_meta("last_cycle") is None
    tmp_db.set_meta("last_cycle", "2026-05-11T09:00:00")
    assert tmp_db.get_meta("last_cycle") == "2026-05-11T09:00:00"
    # Update
    tmp_db.set_meta("last_cycle", "2026-05-18T09:00:00")
    assert tmp_db.get_meta("last_cycle") == "2026-05-18T09:00:00"


# === Reports ===

def test_uae_tax_csv_export(tmp_db, tmp_path):
    from bitcoiners_dca.persistence.reports import export_uae_tax_csv

    tmp_db.record_trade(_make_order(order_id="r-1"))
    tmp_db.record_trade(_make_order(
        order_id="r-2", amount_quote="300", amount_base="0.0008"
    ))

    out_path = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    assert Path(out_path).exists()
    contents = Path(out_path).read_text()
    assert "Date,Exchange,Pair,Side" in contents
    assert "500" in contents
    assert "300" in contents
    assert "Total AED spent" in contents
