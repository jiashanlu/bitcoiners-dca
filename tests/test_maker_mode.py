"""
Maker-mode strategy execution tests — verify limit placement + cancel +
fallback under taker / maker_only / maker_fallback configs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.models import (
    Balance, FeeSchedule, Order, OrderSide, OrderStatus, OrderType, Ticker,
)
from bitcoiners_dca.core.router import SmartRouter
from bitcoiners_dca.core.strategy import DCAStrategy, StrategyConfig
from bitcoiners_dca.exchanges.base import Exchange


class MakerStubExchange(Exchange):
    """Spy exchange that can be configured to fill, never-fill, or fail limits.

    `limit_behavior`:
        "fill"    : place_limit_buy returns FILLED immediately (next get_order).
        "expire"  : place_limit_buy returns PENDING; get_order keeps PENDING.
        "cancel"  : like expire, but cancel transitions to CANCELLED.
    """

    def __init__(
        self,
        name: str,
        ask: str = "300000",
        bid: str = "299000",
        balance_aed: str = "10000",
        limit_behavior: str = "fill",
        gap_fill: str | None = None,
    ):
        self.name = name
        self.dry_run = False
        self._ask = Decimal(ask)
        self._bid = Decimal(bid)
        self._balance = Decimal(balance_aed)
        self._limit_behavior = limit_behavior
        # gap_fill simulates the prod race: get_order keeps reporting the
        # order as PENDING (unfilled) right up to timeout, but the limit
        # actually (fully | partially) fills in the window before the cancel
        # lands, so cancel_order — which returns the authoritative state — is
        # the FIRST place the fill is visible. "full" | "partial" | None.
        self._gap_fill = gap_fill
        self._orders: dict[str, Order] = {}
        self._counter = 0
        self.market_buys: list[tuple[str, Decimal]] = []
        self.limit_buys: list[tuple[str, Decimal, Decimal]] = []
        self.cancels: list[str] = []

    async def health_check(self): return True

    async def get_ticker(self, pair="BTC/AED"):
        return Ticker.from_prices(
            exchange=self.name, pair=pair, bid=self._bid, ask=self._ask,
        )

    async def get_fee_schedule(self, pair="BTC/AED"):
        return FeeSchedule(
            exchange=self.name, pair=pair,
            maker_pct=Decimal("0.001"), taker_pct=Decimal("0.0015"),
            withdrawal_fee_btc=Decimal("0.0002"),
        )

    async def get_balances(self):
        return [Balance(
            exchange=self.name, asset="AED",
            free=self._balance, used=Decimal(0), total=self._balance,
        )]

    async def place_market_buy(self, pair, quote_amount):
        self.market_buys.append((pair, quote_amount))
        self._counter += 1
        base = quote_amount / self._ask
        return Order(
            exchange=self.name, order_id=f"M-{self._counter}", pair=pair,
            side=OrderSide.BUY, type=OrderType.MARKET,
            amount_quote=quote_amount, amount_base=base,
            price_filled_avg=self._ask, fee_quote=quote_amount * Decimal("0.0015"),
            status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc),
        )

    async def place_limit_buy(self, pair, quote_amount, limit_price):
        self.limit_buys.append((pair, quote_amount, limit_price))
        self._counter += 1
        oid = f"L-{self._counter}"
        base = quote_amount / limit_price
        if self._limit_behavior == "fill":
            status = OrderStatus.FILLED
            filled_base = base
        elif self._limit_behavior == "partial":
            status = OrderStatus.PARTIAL
            # Fill half of the requested base amount (simulating maker
            # eating only part of the order before timeout).
            filled_base = base / Decimal(2)
        else:
            status = OrderStatus.PENDING
            filled_base = Decimal(0)
        o = Order(
            exchange=self.name, order_id=oid, pair=pair,
            side=OrderSide.BUY, type=OrderType.LIMIT,
            amount_quote=quote_amount, amount_base=filled_base,
            price_filled_avg=limit_price if status != OrderStatus.PENDING else Decimal(0),
            fee_quote=Decimal(0), status=status,
            created_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc) if status != OrderStatus.PENDING else None,
        )
        self._orders[oid] = o
        return o

    async def cancel_order(self, pair, order_id):
        self.cancels.append(order_id)
        existing = self._orders.get(order_id)
        if not existing:
            return None
        if self._gap_fill and existing.status == OrderStatus.PENDING:
            # The limit filled in the cancel gap — cancel_order surfaces the
            # real state (mirrors ccxt fetch_order after a cancel-that-raced).
            full_base = existing.amount_quote / self._ask
            if self._gap_fill == "full":
                return existing.model_copy(update={
                    "status": OrderStatus.FILLED,
                    "amount_base": full_base,
                    "price_filled_avg": self._ask,
                    "filled_at": datetime.now(timezone.utc),
                })
            if self._gap_fill == "partial":
                return existing.model_copy(update={
                    "status": OrderStatus.CANCELLED,
                    "amount_base": full_base / Decimal(2),
                    "price_filled_avg": self._ask,
                })
        # Return a NEW Order representing the cancellation rather than
        # mutating the stored one. Real adapters do the same — the
        # original placed-order object stays as a record. The strategy
        # snapshots state before this call, but defensive realism here
        # also helps anyone testing other flows against this stub.
        return Order(
            exchange=existing.exchange,
            order_id=existing.order_id,
            pair=existing.pair,
            side=existing.side,
            type=existing.type,
            amount_quote=existing.amount_quote,
            amount_base=existing.amount_base,
            price_filled_avg=existing.price_filled_avg,
            fee_quote=existing.fee_quote,
            status=OrderStatus.CANCELLED,
            created_at=existing.created_at,
            filled_at=existing.filled_at,
        )

    async def get_order(self, pair, order_id):
        return self._orders.get(order_id)

    async def get_trade_history(self, pair="BTC/AED", since=None, limit=100): return []
    async def withdraw_btc(self, amount_btc, address, network="bitcoin"): raise NotImplementedError
    async def get_withdrawal(self, withdrawal_id): raise NotImplementedError


def _cfg(mode: str, timeout: int = 1) -> StrategyConfig:
    return StrategyConfig(
        base_amount_aed=Decimal("500"),
        execution_mode=mode,
        maker_timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_taker_mode_market_buys():
    ex = MakerStubExchange("okx", limit_behavior="fill")
    strategy = DCAStrategy(_cfg("taker"), SmartRouter())
    result = await strategy.execute([ex])
    assert ex.market_buys
    assert ex.limit_buys == []
    assert result.order.type == OrderType.MARKET


@pytest.mark.asyncio
async def test_maker_only_fills_immediately():
    ex = MakerStubExchange("okx", limit_behavior="fill")
    strategy = DCAStrategy(_cfg("maker_only"), SmartRouter())
    result = await strategy.execute([ex])
    assert ex.limit_buys
    assert ex.market_buys == []
    assert result.order.type == OrderType.LIMIT
    assert result.order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_maker_only_skips_when_no_fill():
    ex = MakerStubExchange("okx", limit_behavior="expire")
    strategy = DCAStrategy(_cfg("maker_only", timeout=1), SmartRouter())
    result = await strategy.execute([ex])
    # Limit was placed but never filled; cycle skipped
    assert ex.limit_buys
    assert ex.market_buys == []
    assert result.orders == []
    assert result.errors == []
    # The "skipped" note should be on result
    assert any("timed out" in n.lower() for n in result.notes)


@pytest.mark.asyncio
async def test_maker_fallback_cancels_and_market_buys():
    ex = MakerStubExchange("okx", limit_behavior="expire")
    strategy = DCAStrategy(_cfg("maker_fallback", timeout=1), SmartRouter())
    result = await strategy.execute([ex])
    assert ex.limit_buys
    assert ex.cancels  # cancellation happened
    assert ex.market_buys  # fallback fired
    assert result.order.type == OrderType.MARKET
    assert result.order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_maker_only_partial_fill_keeps_the_partial():
    """Regression: PARTIAL fills used to be discarded entirely (result.orders
    empty) — losing real BTC the maker leg already bought. Now they're
    preserved so the audit trail reflects what actually happened.
    """
    ex = MakerStubExchange("okx", limit_behavior="partial")
    strategy = DCAStrategy(_cfg("maker_only", timeout=1), SmartRouter())
    result = await strategy.execute([ex])

    assert ex.limit_buys
    assert ex.cancels  # unfilled remainder canceled
    assert ex.market_buys == []  # maker_only never market-buys
    assert result.order is not None, "PARTIAL fill was discarded — bug"
    assert result.order.status == OrderStatus.PARTIAL
    assert result.order.amount_base > 0


@pytest.mark.asyncio
async def test_maker_fallback_partial_keeps_partial_no_market_topup():
    """In maker_fallback mode, a PARTIAL fill should be preserved as-is —
    NOT followed by a market-buy of the remainder. Multiple orders behind
    one hop would break downstream accounting; the next cycle catches up.
    """
    ex = MakerStubExchange("okx", limit_behavior="partial")
    strategy = DCAStrategy(_cfg("maker_fallback", timeout=1), SmartRouter())
    result = await strategy.execute([ex])

    assert ex.limit_buys
    assert ex.cancels
    assert ex.market_buys == [], (
        "maker_fallback fired a market-buy on top of a partial fill — "
        "would create two orders behind one hop"
    )
    assert result.order is not None
    assert result.order.status == OrderStatus.PARTIAL


@pytest.mark.asyncio
async def test_maker_fallback_no_double_buy_when_limit_fills_during_cancel():
    """P1 cancel/fill race (audit 2026-06-02): the poll snapshot says PENDING
    right up to timeout, but the limit fully fills in the gap before the
    cancel lands. The bot must use that fill and NOT also market-buy — the
    old code re-bought the full amount, doubling the AED spend.
    """
    ex = MakerStubExchange("okx", limit_behavior="expire", gap_fill="full")
    strategy = DCAStrategy(_cfg("maker_fallback", timeout=1), SmartRouter())
    result = await strategy.execute([ex])

    assert ex.limit_buys
    assert ex.cancels
    assert ex.market_buys == [], (
        "maker_fallback market-bought on top of a limit that filled during "
        "the cancel window — full double-spend"
    )
    assert result.order is not None
    assert result.order.status == OrderStatus.FILLED
    assert result.order.type == OrderType.LIMIT


@pytest.mark.asyncio
async def test_maker_fallback_no_double_buy_when_partial_only_seen_at_cancel():
    """P0 partial double-buy (audit 2026-06-02): get_order reports PENDING
    (filled 0) until timeout, but the order partially filled — visible only
    in the authoritative cancel result. The bot must keep the partial and NOT
    market-buy the full amount on top of the already-bought portion.
    """
    ex = MakerStubExchange("okx", limit_behavior="expire", gap_fill="partial")
    strategy = DCAStrategy(_cfg("maker_fallback", timeout=1), SmartRouter())
    result = await strategy.execute([ex])

    assert ex.limit_buys
    assert ex.cancels
    assert ex.market_buys == [], (
        "maker_fallback re-bought the full amount on top of a partial fill — "
        "~1.6x over-spend (the live P0)"
    )
    assert result.order is not None
    assert result.order.status == OrderStatus.PARTIAL
    assert result.order.amount_base > 0
