"""
End-to-end strategy execution test for multi-hop routes — verifies the
strategy walks both hops, threads the output of hop 1 into hop 2, and
populates ExecutionResult.orders correctly.
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


class TwoHopStubExchange(Exchange):
    """Stub that supports BTC/AED + USDT/AED + BTC/USDT for hop testing."""

    def __init__(
        self,
        name: str,
        prices: dict[str, str],   # pair → ask
        taker: str = "0.0015",
        balances: dict[str, str] | None = None,
    ):
        self.name = name
        self.dry_run = False
        self._prices = {k: Decimal(v) for k, v in prices.items()}
        self._taker = Decimal(taker)
        self._balances = {k: Decimal(v) for k, v in (balances or {}).items()}
        self.buys: list[tuple[str, Decimal]] = []

    async def health_check(self): return True

    async def get_ticker(self, pair="BTC/AED"):
        if pair not in self._prices:
            raise ValueError(f"{self.name} does not list {pair}")
        return Ticker.from_prices(
            exchange=self.name, pair=pair,
            bid=self._prices[pair] - Decimal("0.01"),
            ask=self._prices[pair],
        )

    async def get_fee_schedule(self, pair="BTC/AED"):
        return FeeSchedule(
            exchange=self.name, pair=pair,
            maker_pct=self._taker / 2, taker_pct=self._taker,
            withdrawal_fee_btc=Decimal("0.0002"),
        )

    async def get_balances(self):
        return [
            Balance(exchange=self.name, asset=a,
                    free=v, used=Decimal(0), total=v)
            for a, v in self._balances.items() if v > 0
        ]

    async def place_market_buy(self, pair, quote_amount):
        if pair not in self._prices:
            raise ValueError(f"{self.name} cannot buy {pair}")
        self.buys.append((pair, quote_amount))
        ask = self._prices[pair]
        # Mimic a market-buy: receive base = quote / (ask * (1 + taker))
        base_received = quote_amount / (ask * (Decimal(1) + self._taker))
        return Order(
            exchange=self.name,
            order_id=f"{self.name}-{pair}-{len(self.buys)}",
            pair=pair, side=OrderSide.BUY, type=OrderType.MARKET,
            amount_quote=quote_amount, amount_base=base_received,
            price_filled_avg=ask,
            fee_quote=quote_amount * self._taker,
            status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc),
        )

    async def get_order(self, pair, order_id): raise NotImplementedError
    async def get_trade_history(self, pair="BTC/AED", since=None, limit=100): return []
    async def withdraw_btc(self, amount_btc, address, network="bitcoin"): raise NotImplementedError
    async def get_withdrawal(self, withdrawal_id): raise NotImplementedError


@pytest.mark.asyncio
async def test_strategy_executes_two_hop_route():
    okx = TwoHopStubExchange("okx", prices={
        "BTC/AED":  "301050",
        "USDT/AED": "3.665",
        "BTC/USDT": "81934.6",
    }, balances={"AED": "100000"})

    cfg = StrategyConfig(base_amount_aed=Decimal("1000"), pair="BTC/AED")
    router = SmartRouter(enable_two_hop=True, intermediates=["USDT"])
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([okx])

    # Should have placed TWO orders: USDT buy then BTC buy
    assert len(result.orders) == 2
    assert result.orders[0].pair == "USDT/AED"
    assert result.orders[1].pair == "BTC/USDT"

    # Hop 1 spent 1000 AED, received some USDT
    assert result.orders[0].amount_quote == Decimal("1000")
    usdt_received = result.orders[0].amount_base
    assert usdt_received > 0

    # Hop 2's input should match hop 1's output (within rounding)
    assert result.orders[1].amount_quote == usdt_received

    # Final order has BTC as base
    assert result.orders[1].amount_base > 0
    assert result.order is result.orders[-1]
    assert result.errors == []


@pytest.mark.asyncio
async def test_strategy_falls_back_to_direct_when_two_hop_disabled():
    okx = TwoHopStubExchange("okx", prices={
        "BTC/AED":  "301050",
        "USDT/AED": "3.665",
        "BTC/USDT": "81934.6",
    }, balances={"AED": "100000"})

    cfg = StrategyConfig(base_amount_aed=Decimal("1000"))
    router = SmartRouter(enable_two_hop=False)
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([okx])

    assert len(result.orders) == 1
    assert result.orders[0].pair == "BTC/AED"


@pytest.mark.asyncio
async def test_intermediate_direct_sizes_order_in_usdt_not_aed():
    """Audit 2026-06-02 / task #212: an intermediate-direct route funded from
    idle USDT must place a USDT-denominated order sized from the AED budget
    (1000 AED ≈ 272 USDT at 3.67), NOT spend 1000 USDT (~3670 AED, ~3.67x).
    """
    usdt_ask = Decimal("3.67")
    okx = TwoHopStubExchange("okx", prices={
        "BTC/AED":  "367000",
        "USDT/AED": str(usdt_ask),
        "BTC/USDT": "100000",
    }, balances={"AED": "5000", "USDT": "500"})

    cfg = StrategyConfig(base_amount_aed=Decimal("1000"), pair="BTC/AED")
    router = SmartRouter(
        enable_two_hop=True,
        intermediates=["USDT"],
        prefer_intermediate_balance=True,
        prefer_intermediate_min=Decimal("10"),
    )
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([okx])
    assert result.errors == []

    # Intermediate-direct wins (prefer-stablecoin nudge): a single BTC/USDT buy.
    btc_usdt_buys = [b for b in okx.buys if b[0] == "BTC/USDT"]
    assert btc_usdt_buys, f"expected a BTC/USDT buy, got {okx.buys}"
    pair, spent = btc_usdt_buys[0]

    # Spent in USDT ≈ 1000 AED / 3.67 ≈ 272, NOT 1000.
    assert spent < Decimal("300"), (
        f"order sized in AED not USDT — spent {spent} USDT for a 1000-AED "
        f"budget (~3.67x over-spend)"
    )
    expected_usdt = Decimal("1000") / usdt_ask
    assert abs(spent - expected_usdt) < Decimal("1")
    # Never exceeds the held idle USDT balance.
    assert spent <= Decimal("500")
