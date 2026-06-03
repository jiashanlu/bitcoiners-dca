"""
Tax-CSV report tests — pin the per-row AED cost-basis column and the
BTC-cost-basis summary line against the shared per-stablecoin rate helper
(task #210). This is money-accounting: the CSV's stable→AED conversion MUST
match Database.btc_cost_basis_aed exactly, never blend USDT with USDC.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bitcoiners_dca.core.models import Order, OrderSide, OrderStatus, OrderType
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.persistence.reports import export_uae_tax_csv


@pytest.fixture
def tmp_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    yield db
    db.close()


def _hop(pair, order_id, amount_quote, amount_base, fee="0"):
    return Order(
        exchange="okx", order_id=order_id, pair=pair,
        side=OrderSide.BUY, type=OrderType.MARKET,
        amount_quote=Decimal(amount_quote),
        amount_base=Decimal(amount_base),
        price_filled_avg=Decimal(amount_quote) / Decimal(amount_base),
        fee_quote=Decimal(fee), status=OrderStatus.FILLED,
        created_at=datetime.now(timezone.utc),
        filled_at=datetime.now(timezone.utc),
    )


def _read_rows(path: Path) -> list[list[str]]:
    with Path(path).open(newline="") as f:
        return list(csv.reader(f))


def _data_rows(rows: list[list[str]], header: list[str]) -> list[dict]:
    """Rows between the header and the SUMMARY block, as dicts keyed by header."""
    out = []
    started = False
    for r in rows:
        if r == header:
            started = True
            continue
        if not started:
            continue
        if not r or r[0].startswith("==="):
            break
        out.append(dict(zip(header, r)))
    return out


HEADER = [
    "Date", "Exchange", "Pair", "Side", "Amount (AED)", "Amount (BTC)",
    "Price (AED per BTC)", "Fee", "Fee Ccy", "AED Cost Basis", "Order ID",
]


def test_csv_has_aed_cost_basis_column(tmp_db, tmp_path):
    tmp_db.record_trade(_hop("BTC/AED", "d-1", "500", "0.0014"))
    out = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    rows = _read_rows(out)
    assert HEADER in rows


def test_direct_btc_aed_cost_basis_is_face_value(tmp_db, tmp_path):
    """A BTC/AED leg's per-row cost basis is just the AED outlay."""
    tmp_db.record_trade(_hop("BTC/AED", "d-1", "500", "0.0014"))
    out = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    data = _data_rows(_read_rows(out), HEADER)
    [row] = [r for r in data if r["Order ID"] == "d-1"]
    assert row["AED Cost Basis"] == "500.00"


def test_per_row_cost_basis_equals_quote_times_stable_rate(tmp_db, tmp_path):
    """The core money assertion: a BTC/<stable> row's AED Cost Basis cell
    equals amount_quote (in the stablecoin) × that stablecoin's weighted
    AED rate — sourced from the SAME db.stable_aed_rates() helper the CSV
    and db.btc_cost_basis_aed share.

    Setup: USDT/AED 370→100 (rate 3.70), BTC/USDT 50 USDT → 0.00015 BTC.
    Expected cell = 50 × 3.70 = 185.00.
    """
    tmp_db.record_trade(_hop("USDT/AED", "u-1", "370", "100"))
    tmp_db.record_trade(_hop("BTC/USDT", "b-1", "50", "0.00015"))

    rate = tmp_db.stable_aed_rates()["USDT"]
    expected = Decimal("50") * rate  # 185.00

    out = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    data = _data_rows(_read_rows(out), HEADER)
    [btc_usdt] = [r for r in data if r["Order ID"] == "b-1"]
    assert btc_usdt["AED Cost Basis"] == f"{expected:.2f}" == "185.00"

    # The USDT/AED funding leg itself is NOT a BTC-receiving leg → blank.
    [usdt_aed] = [r for r in data if r["Order ID"] == "u-1"]
    assert usdt_aed["AED Cost Basis"] == ""


def test_usdc_row_uses_usdc_rate_not_usdt(tmp_db, tmp_path):
    """USDT and USDC rates must stay separate at the CSV row level too —
    a BTC/USDC row uses 3.80, a BTC/USDT row uses 3.70, never blended."""
    tmp_db.record_trade(_hop("USDT/AED", "u-1", "370", "100"))   # 3.70
    tmp_db.record_trade(_hop("USDC/AED", "uc-1", "380", "100"))  # 3.80
    tmp_db.record_trade(_hop("BTC/USDT", "b-1", "50", "0.00015"))
    tmp_db.record_trade(_hop("BTC/USDC", "bc-1", "50", "0.00015"))

    out = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    data = _data_rows(_read_rows(out), HEADER)
    [via_usdt] = [r for r in data if r["Order ID"] == "b-1"]
    [via_usdc] = [r for r in data if r["Order ID"] == "bc-1"]
    assert via_usdt["AED Cost Basis"] == "185.00"   # 50 × 3.70
    assert via_usdc["AED Cost Basis"] == "190.00"   # 50 × 3.80


def test_summary_btc_cost_basis_matches_db_helper(tmp_db, tmp_path):
    """The lifetime CSV's "BTC cost basis (AED)" summary line must equal
    Database.btc_cost_basis_aed() — the single shared source of truth."""
    tmp_db.record_trade(_hop("BTC/AED", "d-1", "500", "0.0017"))
    tmp_db.record_trade(_hop("USDT/AED", "u-1", "370", "100"))
    tmp_db.record_trade(_hop("BTC/USDT", "b-1", "50", "0.00015"))
    tmp_db.record_trade(_hop("USDC/AED", "uc-1", "380", "100"))
    tmp_db.record_trade(_hop("BTC/USDC", "bc-1", "50", "0.00015"))

    expected = tmp_db.btc_cost_basis_aed()  # 500 + 185 + 190 = 875
    assert expected == Decimal("875")

    out = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    contents = Path(out).read_text()
    assert f"BTC cost basis (AED),{expected:.2f}" in contents
    assert "BTC cost basis (AED),875.00" in contents


def test_summary_cost_basis_excludes_idle_stable_inventory(tmp_db, tmp_path):
    """Idle stablecoin inventory inflates "Total AED spent" but must NOT
    inflate "BTC cost basis (AED)"."""
    tmp_db.record_trade(_hop("USDT/AED", "u-1", "3677", "1000"))
    tmp_db.record_trade(_hop("BTC/USDT", "b-1", "200", "0.0006"))

    out = export_uae_tax_csv(tmp_db, str(tmp_path), year=None)
    contents = Path(out).read_text()
    # raw outflow 3677 vs cost basis 200 × 3.677 = 735.40
    assert "Total AED spent (buys),3677.00" in contents
    assert "BTC cost basis (AED),735.40" in contents
