"""
Router unit tests — verify smart-routing math without hitting any real APIs.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

import pytest

from bitcoiners_dca.core.models import (
    Balance, FeeSchedule, Order, Ticker, Withdrawal,
)
from bitcoiners_dca.core.router import SmartRouter
from bitcoiners_dca.exchanges.base import Exchange


# === FAKE EXCHANGE FOR TESTING ===

class FakeExchange(Exchange):
    """Stub Exchange that returns canned tickers + fees — no network."""

    def __init__(
        self,
        name: str,
        ask: str,
        bid: str,
        taker: str = "0.001",
        quote_balance: str = "1000000",  # default: unlimited AED
    ):
        self.name = name
        self._ask = Decimal(ask)
        self._bid = Decimal(bid)
        self._taker = Decimal(taker)
        self._quote_balance = Decimal(quote_balance)

    async def health_check(self) -> bool: return True

    async def get_ticker(self, pair: str = "BTC/AED") -> Ticker:
        return Ticker.from_prices(
            exchange=self.name, pair=pair, bid=self._bid, ask=self._ask,
        )

    async def get_fee_schedule(self, pair: str = "BTC/AED") -> FeeSchedule:
        return FeeSchedule(
            exchange=self.name, pair=pair,
            maker_pct=self._taker / 2, taker_pct=self._taker,
            withdrawal_fee_btc=Decimal("0.0002"),
        )

    async def get_balances(self):
        from bitcoiners_dca.core.models import Balance
        return [Balance(
            exchange=self.name, asset="AED",
            free=self._quote_balance, used=Decimal("0"), total=self._quote_balance,
        )]

    async def place_market_buy(self, pair, quote_amount): raise NotImplementedError
    async def get_order(self, pair, order_id): raise NotImplementedError
    async def get_trade_history(self, pair="BTC/AED", since=None, limit=100): return []
    async def withdraw_btc(self, amount_btc, address, network="bitcoin"): raise NotImplementedError
    async def get_withdrawal(self, withdrawal_id): raise NotImplementedError


# === TESTS ===

def _first_hop_exchange(decision) -> str:
    return decision.chosen.route.hops[0].exchange


@pytest.mark.asyncio
async def test_picks_lowest_effective_price():
    """OKX cheaper than BitOasis by spot — but BitOasis's lower fee can sometimes win."""
    okx = FakeExchange("okx",      ask="350000", bid="349900", taker="0.0015")
    bo  = FakeExchange("bitoasis", ask="351000", bid="350800", taker="0.005")

    # OKX: 350000 * 1.0015 = 350525
    # BitOasis: 351000 * 1.005 = 352755
    decision = await SmartRouter().pick([okx, bo])

    assert _first_hop_exchange(decision) == "okx"
    assert decision.chosen.effective_price == Decimal("350525")
    assert decision.best_alt.route.hops[0].exchange == "bitoasis"


@pytest.mark.asyncio
async def test_fees_can_flip_the_winner():
    okx = FakeExchange("okx",      ask="350000", bid="349900", taker="0.01")
    bo  = FakeExchange("bitoasis", ask="350100", bid="350000", taker="0.001")
    decision = await SmartRouter().pick([okx, bo])
    assert _first_hop_exchange(decision) == "bitoasis"


@pytest.mark.asyncio
async def test_preferred_exchange_bonus():
    a = FakeExchange("a", ask="350000", bid="349900", taker="0.001")
    b = FakeExchange("b", ask="350100", bid="350000", taker="0.001")
    decision = await SmartRouter(
        preferred_exchange="b", preferred_bonus_pct=Decimal("1.0")
    ).pick([a, b])
    assert _first_hop_exchange(decision) == "b"


@pytest.mark.asyncio
async def test_excludes_wide_spread():
    tight = FakeExchange("tight",  ask="350000", bid="349900")
    wide  = FakeExchange("wide",   ask="340000", bid="320000")
    decision = await SmartRouter(
        exclude_if_spread_pct_above=Decimal("2.0"),
    ).pick([tight, wide])
    assert _first_hop_exchange(decision) == "tight"


@pytest.mark.asyncio
async def test_balance_aware_skips_underfunded_winner():
    """Cheapest exchange has no AED — router falls back to next-best funded one."""
    cheap_but_empty = FakeExchange("okx", ask="350000", bid="349900",
                                    taker="0.001", quote_balance="0")
    pricey_funded   = FakeExchange("bitoasis", ask="351000", bid="350900",
                                    taker="0.001", quote_balance="5000")

    decision = await SmartRouter().pick(
        [cheap_but_empty, pricey_funded],
        required_quote_amount=Decimal("500"),
    )
    assert decision.chosen.route.hops[0].exchange == "bitoasis"
    assert decision.chosen.quote_balance == Decimal("5000")


@pytest.mark.asyncio
async def test_balance_aware_picks_cheapest_when_both_funded():
    cheap = FakeExchange("okx", ask="350000", bid="349900",
                         taker="0.001", quote_balance="1000")
    pricey = FakeExchange("bitoasis", ask="360000", bid="359900",
                          taker="0.001", quote_balance="5000")

    decision = await SmartRouter().pick(
        [cheap, pricey], required_quote_amount=Decimal("500"),
    )
    assert decision.chosen.route.hops[0].exchange == "okx"


@pytest.mark.asyncio
async def test_balance_aware_falls_back_when_all_underfunded():
    """If every exchange is short, the bot prefers the exchange with the
    MOST usable quote balance — not the one with the cheapest price. A
    near-empty venue priced 0.1% better is worthless if it can't fund the
    buy. The strategy's post-route balance-clamp trims the ask to what's
    actually available. Prevents a scenario where OKX has 37 AED + the
    best price keeps winning while BitOasis has 1189 AED sitting idle.
    """
    cheap_but_empty = FakeExchange("a", ask="350000", bid="349900", quote_balance="100")
    pricier_but_funded = FakeExchange("b", ask="360000", bid="359900", quote_balance="200")

    decision = await SmartRouter().pick(
        [cheap_but_empty, pricier_but_funded], required_quote_amount=Decimal("1000"),
    )
    assert decision.chosen.route.hops[0].exchange == "b"


@pytest.mark.asyncio
async def test_required_amount_none_keeps_old_behaviour():
    """No required_quote_amount → balance check is skipped entirely."""
    empty = FakeExchange("okx", ask="350000", bid="349900", quote_balance="0")
    funded = FakeExchange("bitoasis", ask="351000", bid="350900", quote_balance="5000")

    decision = await SmartRouter().pick([empty, funded])  # no required_quote_amount
    # Without balance check, cheapest wins even if it's empty
    assert decision.chosen.route.hops[0].exchange == "okx"


@pytest.mark.asyncio
async def test_price_premium_calc():
    """price_premium_vs_alt_pct correctly reports savings."""
    cheap = FakeExchange("cheap", ask="350000", bid="349900", taker="0.001")
    pricey = FakeExchange("pricey", ask="360000", bid="359900", taker="0.001")

    decision = await SmartRouter().pick([cheap, pricey])
    premium = decision.price_premium_vs_alt_pct()
    # Saved ~2.86% by picking cheap
    assert Decimal("2.5") < premium < Decimal("3.2"), f"Got {premium}"
