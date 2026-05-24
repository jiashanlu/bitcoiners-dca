"""
Order.effective_fee_quote — derives an AED-equivalent fee from fee_base
when the exchange charged in the base asset (OKX does this on AED-
quoted pairs, returning fee_base=BTC). Without this derivation, the
tax CSV reads fee_quote and silently lost every AED-pair cycle's fee.

Audit follow-up 2026-05-24.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bitcoiners_dca.core.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)


def _make_order(*, fee_base: Decimal, fee_quote: Decimal, price_filled_avg: Decimal | None) -> Order:
    return Order(
        exchange="okx",
        order_id="x",
        pair="BTC/AED",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        amount_quote=Decimal("16.44"),
        amount_base=Decimal("0.0000567"),
        price_filled_avg=price_filled_avg,
        fee_base=fee_base,
        fee_quote=fee_quote,
        status=OrderStatus.FILLED,
        created_at=datetime.now(timezone.utc),
    )


def test_uses_fee_quote_when_present():
    """Exchange charged in quote currency directly (e.g. some Binance
    paths) — just return that value."""
    o = _make_order(
        fee_base=Decimal("0"),
        fee_quote=Decimal("0.025"),
        price_filled_avg=Decimal("282000"),
    )
    assert o.effective_fee_quote == Decimal("0.025")


def test_derives_from_fee_base_when_quote_is_zero():
    """OKX AED-pair case: fee returned in BTC. Convert via fill price."""
    o = _make_order(
        fee_base=Decimal("0.00000034"),
        fee_quote=Decimal("0"),
        price_filled_avg=Decimal("282161"),
    )
    # 0.00000034 BTC * 282161 AED/BTC ≈ 0.0959 AED
    expected = Decimal("0.00000034") * Decimal("282161")
    assert o.effective_fee_quote == expected


def test_zero_when_no_fee_info():
    o = _make_order(
        fee_base=Decimal("0"),
        fee_quote=Decimal("0"),
        price_filled_avg=Decimal("282000"),
    )
    assert o.effective_fee_quote == Decimal("0")


def test_zero_when_fee_base_present_but_no_price():
    """Can't derive AED equivalent without a fill price — return 0
    rather than guess. Strategy/CLI paths that pre-populate
    price_filled_avg won't hit this."""
    o = _make_order(
        fee_base=Decimal("0.00000034"),
        fee_quote=Decimal("0"),
        price_filled_avg=None,
    )
    assert o.effective_fee_quote == Decimal("0")


def test_fee_quote_takes_precedence_over_fee_base():
    """If somehow BOTH are populated (shouldn't happen but defensive),
    trust the explicit quote value over the derived one — exchange
    knows its own currency better than our conversion."""
    o = _make_order(
        fee_base=Decimal("0.00000034"),
        fee_quote=Decimal("0.123"),
        price_filled_avg=Decimal("282000"),
    )
    assert o.effective_fee_quote == Decimal("0.123")
