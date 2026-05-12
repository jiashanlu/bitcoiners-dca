"""
DCAStrategy unit tests — verify buy decision, dip overlay, routing pass-through,
and auto-withdraw threshold logic without hitting any real APIs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import pytest

from bitcoiners_dca.core.models import (
    Balance, FeeSchedule, Order, OrderSide, OrderStatus, OrderType,
    Ticker, Withdrawal, WithdrawalStatus,
)
from bitcoiners_dca.core.router import SmartRouter
from bitcoiners_dca.core.strategy import DCAStrategy, StrategyConfig
from bitcoiners_dca.exchanges.base import Exchange


class StubExchange(Exchange):
    """Fake exchange that records every call. No network."""

    def __init__(
        self,
        name: str,
        ask: str = "350000",
        bid: str = "349900",
        taker: str = "0.001",
        btc_balance: str = "0",
        fee_btc: str = "0.0002",
    ):
        self.name = name
        self.dry_run = False
        self._ask = Decimal(ask)
        self._bid = Decimal(bid)
        self._taker = Decimal(taker)
        self._btc_balance = Decimal(btc_balance)
        self._fee_btc = Decimal(fee_btc)

        # Spy hooks
        self.buys: list[tuple[str, Decimal]] = []
        self.withdrawals: list[tuple[Decimal, str, str]] = []

    async def health_check(self): return True

    async def get_ticker(self, pair="BTC/AED"):
        return Ticker.from_prices(
            exchange=self.name, pair=pair, bid=self._bid, ask=self._ask,
        )

    async def get_fee_schedule(self, pair="BTC/AED"):
        return FeeSchedule(
            exchange=self.name, pair=pair,
            maker_pct=self._taker / 2, taker_pct=self._taker,
            withdrawal_fee_btc=self._fee_btc,
        )

    async def get_balances(self):
        return [Balance(
            exchange=self.name, asset="BTC",
            free=self._btc_balance, used=Decimal("0"), total=self._btc_balance,
        )] if self._btc_balance > 0 else []

    async def place_market_buy(self, pair, quote_amount):
        self.buys.append((pair, quote_amount))
        amount_base = quote_amount / self._ask
        # Pretend the new BTC lands in our balance (so auto-withdraw can see it)
        self._btc_balance += amount_base
        return Order(
            exchange=self.name,
            order_id=f"{self.name}-{len(self.buys)}",
            pair=pair, side=OrderSide.BUY, type=OrderType.MARKET,
            amount_quote=quote_amount, amount_base=amount_base,
            price_filled_avg=self._ask,
            fee_quote=quote_amount * self._taker,
            status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc),
        )

    async def get_order(self, pair, order_id): raise NotImplementedError
    async def get_trade_history(self, pair="BTC/AED", since=None, limit=100): return []

    async def withdraw_btc(self, amount_btc, address, network="bitcoin"):
        self.withdrawals.append((amount_btc, address, network))
        return Withdrawal(
            exchange=self.name, withdrawal_id=f"w-{len(self.withdrawals)}",
            asset="BTC", amount=amount_btc, address=address,
            fee=self._fee_btc, status=WithdrawalStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )

    async def get_withdrawal(self, withdrawal_id): raise NotImplementedError


@pytest.fixture
def base_config():
    return StrategyConfig(base_amount_aed=Decimal("500"))


@pytest.fixture
def router():
    return SmartRouter()


# === Buy decision ===

@pytest.mark.asyncio
async def test_baseline_buy_routes_and_executes(base_config, router):
    ex = StubExchange("okx", ask="350000")
    strategy = DCAStrategy(base_config, router)

    result = await strategy.execute([ex])

    assert result.errors == []
    assert result.intended_amount_aed == Decimal("500")
    assert result.overlay_applied is None
    assert ex.buys == [("BTC/AED", Decimal("500"))]
    assert result.order is not None
    assert result.order.amount_base > 0


@pytest.mark.asyncio
async def test_picks_cheaper_exchange(base_config, router):
    cheap = StubExchange("cheap", ask="350000")
    pricey = StubExchange("pricey", ask="360000")
    strategy = DCAStrategy(base_config, router)

    result = await strategy.execute([cheap, pricey])

    assert cheap.buys == [("BTC/AED", Decimal("500"))]
    assert pricey.buys == []
    assert result.routing_decision.chosen.route.hops[0].exchange == "cheap"


# === Dip overlay ===

@pytest.mark.asyncio
async def test_dip_overlay_triggers_multiplier(router):
    """If current price is 15% below the lookback reference, multiplier kicks in."""
    cfg = StrategyConfig(
        base_amount_aed=Decimal("500"),
        dip_overlay_enabled=True,
        dip_threshold_pct=Decimal("-10"),
        dip_multiplier=Decimal("2.0"),
    )
    ex = StubExchange("okx", ask="297500")  # 15% below 350000
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([ex], historical_price_7d_ago=Decimal("350000"))

    assert result.intended_amount_aed == Decimal("1000")  # 2x multiplier applied
    assert "buy-the-dip" in (result.overlay_applied or "")
    assert ex.buys == [("BTC/AED", Decimal("1000"))]


@pytest.mark.asyncio
async def test_dip_overlay_skips_when_above_threshold(router):
    """Price only down 5% — below the -10% trigger → no multiplier."""
    cfg = StrategyConfig(
        base_amount_aed=Decimal("500"),
        dip_overlay_enabled=True,
        dip_threshold_pct=Decimal("-10"),
        dip_multiplier=Decimal("2.0"),
    )
    ex = StubExchange("okx", ask="332500")  # 5% below 350000
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([ex], historical_price_7d_ago=Decimal("350000"))

    assert result.intended_amount_aed == Decimal("500")
    assert result.overlay_applied is None


# === Auto-withdraw ===

@pytest.mark.asyncio
async def test_auto_withdraw_fires_when_threshold_met(router):
    cfg = StrategyConfig(
        base_amount_aed=Decimal("500"),
        auto_withdraw_enabled=True,
        auto_withdraw_address="bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        auto_withdraw_threshold_btc=Decimal("0.001"),
    )
    # Start with 0.001 BTC + 0.5 AED worth ≈ extra → above threshold
    ex = StubExchange("okx", ask="350000", btc_balance="0.001")
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([ex])

    assert len(ex.withdrawals) == 1
    amount, address, network = ex.withdrawals[0]
    assert address == "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
    assert amount > 0
    assert result.withdrew_btc == amount


@pytest.mark.asyncio
async def test_auto_withdraw_skips_under_threshold(router):
    cfg = StrategyConfig(
        base_amount_aed=Decimal("500"),
        auto_withdraw_enabled=True,
        auto_withdraw_address="bc1qabc",
        auto_withdraw_threshold_btc=Decimal("1.0"),  # very high
    )
    ex = StubExchange("okx", ask="350000", btc_balance="0")
    strategy = DCAStrategy(cfg, router)

    result = await strategy.execute([ex])

    assert ex.withdrawals == []
    assert result.withdrew_btc is None


@pytest.mark.asyncio
async def test_auto_withdraw_disabled_by_default(router, base_config):
    """No auto_withdraw_address even on big balance — no withdrawal."""
    ex = StubExchange("okx", ask="350000", btc_balance="10")
    strategy = DCAStrategy(base_config, router)

    result = await strategy.execute([ex])

    assert ex.withdrawals == []
    assert result.withdrew_btc is None
