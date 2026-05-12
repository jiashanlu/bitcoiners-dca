"""
Limit-order primitive tests — verify each adapter's dry-run limit-buy flow,
cancel flow, and the ABC wait_for_fill polling helper.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.models import (
    Order, OrderSide, OrderStatus, OrderType,
)
from bitcoiners_dca.exchanges.base import Exchange


class FakeLimitExchange(Exchange):
    """Spy exchange that lets tests control fill timing for wait_for_fill."""

    def __init__(self, fill_after_polls: int = 0):
        self.name = "fake"
        self.dry_run = False
        self._calls = 0
        self._fill_after = fill_after_polls
        self._pending: dict[str, Order] = {}

    async def health_check(self): return True
    async def get_ticker(self, pair="BTC/AED"): raise NotImplementedError
    async def get_fee_schedule(self, pair="BTC/AED"): raise NotImplementedError
    async def get_balances(self): return []
    async def place_market_buy(self, pair, quote_amount): raise NotImplementedError
    async def get_trade_history(self, pair="BTC/AED", since=None, limit=100): return []
    async def withdraw_btc(self, amount_btc, address, network="bitcoin"): raise NotImplementedError
    async def get_withdrawal(self, withdrawal_id): raise NotImplementedError

    async def place_limit_buy(self, pair, quote_amount, limit_price):
        now = datetime.now(timezone.utc)
        o = Order(
            exchange=self.name, order_id="L-1", pair=pair,
            side=OrderSide.BUY, type=OrderType.LIMIT,
            amount_quote=quote_amount,
            amount_base=quote_amount / limit_price,
            price_filled_avg=Decimal(0),
            fee_quote=Decimal(0),
            status=OrderStatus.PENDING,
            created_at=now, filled_at=None,
        )
        self._pending[o.order_id] = o
        return o

    async def cancel_order(self, pair, order_id):
        o = self._pending.get(order_id)
        if o:
            o.status = OrderStatus.CANCELLED
        return o

    async def get_order(self, pair, order_id):
        self._calls += 1
        o = self._pending.get(order_id)
        if o and self._calls > self._fill_after:
            o.status = OrderStatus.FILLED
            o.filled_at = datetime.now(timezone.utc)
            o.price_filled_avg = o.amount_quote / o.amount_base
        return o


# === wait_for_fill ===

@pytest.mark.asyncio
async def test_wait_for_fill_returns_when_filled():
    ex = FakeLimitExchange(fill_after_polls=2)
    placed = await ex.place_limit_buy("BTC/AED", Decimal("100"), Decimal("300000"))

    final = await ex.wait_for_fill(
        "BTC/AED", placed.order_id,
        timeout_seconds=5, poll_interval_seconds=0.05,
    )

    assert final.status == OrderStatus.FILLED
    assert final.price_filled_avg > 0


@pytest.mark.asyncio
async def test_wait_for_fill_times_out_pending():
    ex = FakeLimitExchange(fill_after_polls=999)  # never fills
    placed = await ex.place_limit_buy("BTC/AED", Decimal("100"), Decimal("300000"))

    final = await ex.wait_for_fill(
        "BTC/AED", placed.order_id,
        timeout_seconds=0.2, poll_interval_seconds=0.05,
    )

    assert final.status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_changes_status():
    ex = FakeLimitExchange(fill_after_polls=999)
    placed = await ex.place_limit_buy("BTC/AED", Decimal("100"), Decimal("300000"))
    cancelled = await ex.cancel_order("BTC/AED", placed.order_id)
    assert cancelled.status == OrderStatus.CANCELLED


# === Dry-run adapter integration ===

@pytest.mark.asyncio
async def test_okx_dry_run_limit_buy():
    from bitcoiners_dca.exchanges.okx import OKXExchange
    ex = OKXExchange(api_key="x", api_secret="x", passphrase="x", dry_run=True)
    o = await ex.place_limit_buy("BTC/USDT", Decimal("100"), Decimal("80000"))
    # Dry-run limit buys simulate the happy path (filled at the limit price)
    # so wait_for_fill in maker_fallback doesn't make a live get_order call.
    assert o.status == OrderStatus.FILLED
    assert o.type == OrderType.LIMIT
    assert o.amount_base == Decimal("100") / Decimal("80000")
    await ex.close()


@pytest.mark.asyncio
async def test_binance_dry_run_limit_buy():
    from bitcoiners_dca.exchanges.binance import BinanceExchange
    ex = BinanceExchange(api_key="x", api_secret="x", dry_run=True)
    o = await ex.place_limit_buy("BTC/USDT", Decimal("100"), Decimal("80000"))
    # Dry-run limit buys simulate the happy path (filled at the limit price)
    # so wait_for_fill in maker_fallback doesn't make a live get_order call.
    assert o.status == OrderStatus.FILLED
    assert o.type == OrderType.LIMIT
    await ex.close()


@pytest.mark.asyncio
async def test_bitoasis_dry_run_limit_buy():
    from bitcoiners_dca.exchanges.bitoasis import BitOasisExchange
    ex = BitOasisExchange(api_token="dummy", dry_run=True)
    o = await ex.place_limit_buy("BTC/AED", Decimal("500"), Decimal("300000"))
    # Dry-run limit buys simulate the happy path (filled at the limit price)
    # so wait_for_fill in maker_fallback doesn't make a live get_order call.
    assert o.status == OrderStatus.FILLED
    assert o.type == OrderType.LIMIT
    # BitOasis rounds to 8dp
    assert o.amount_base == Decimal("0.00166667")
    await ex.close()
