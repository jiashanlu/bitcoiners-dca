"""
Regression: the BitOasis adapter must override `cancel_all_open_orders`
because the inherited base implementation calls ccxt's `fetch_open_orders`,
which fails silently on BitOasis's httpx-backed client and would let
stale maker_fallback orders pile up across cycles, locking AED.

The pre-cycle sweep (strategy.execute → cancel_all_open_orders for each
of {BTC/AED, USDT/AED, BTC/USDT} on every exchange) relies on this.
"""
from __future__ import annotations

import asyncio
import pytest

from bitcoiners_dca.exchanges.bitoasis import BitOasisExchange


class _FakeBitOasis(BitOasisExchange):
    """BitOasis subclass that fakes the network layer so we can assert
    behaviour without an httpx round-trip."""

    def __init__(self, open_orders, cancel_should_raise=False):
        # Skip the parent __init__ (which constructs an httpx.AsyncClient).
        # We don't need the client — `_request` is overridden below.
        self.dry_run = True
        self._open_orders = open_orders
        self._cancel_calls: list[str] = []
        self._cancel_should_raise = cancel_should_raise
        self._request_calls: list[tuple[str, str, dict]] = []

    async def _request(self, method, path, params=None, body=None, authenticated=True):
        self._request_calls.append((method, path, params or {}))
        return {"orders": self._open_orders}

    async def cancel_order(self, pair, order_id):
        self._cancel_calls.append(order_id)
        if self._cancel_should_raise:
            raise RuntimeError("simulated cancel failure")
        return {"id": order_id, "status": "CANCELLED"}


@pytest.mark.asyncio
async def test_sweep_cancels_every_open_order():
    ex = _FakeBitOasis(
        open_orders=[
            {"id": "order-1", "side": "BUY", "type": "LIMIT"},
            {"id": "order-2", "side": "BUY", "type": "LIMIT"},
            {"id": "order-3", "side": "BUY", "type": "LIMIT"},
        ],
    )
    n = await ex.cancel_all_open_orders("BTC/AED")
    assert n == 3
    assert ex._cancel_calls == ["order-1", "order-2", "order-3"]
    # Should hit BitOasis's own endpoint (not ccxt fetch_open_orders).
    assert ex._request_calls == [
        ("GET", "/exchange/orders/BTC-AED", {"status": "OPEN"}),
    ]


@pytest.mark.asyncio
async def test_sweep_handles_empty_open_orders():
    ex = _FakeBitOasis(open_orders=[])
    n = await ex.cancel_all_open_orders("BTC/AED")
    assert n == 0
    assert ex._cancel_calls == []


@pytest.mark.asyncio
async def test_sweep_continues_when_individual_cancel_fails():
    """If one order cancel raises, others should still be attempted.
    Pre-cycle sweep can't bail on the first error or one bad order
    poisons the rest of the cycle."""
    ex = _FakeBitOasis(
        open_orders=[
            {"id": "order-1"},
            {"id": "order-2"},
        ],
        cancel_should_raise=True,
    )
    n = await ex.cancel_all_open_orders("BTC/AED")
    # All cancels raised → n stays 0, but we attempted both.
    assert n == 0
    assert ex._cancel_calls == ["order-1", "order-2"]
