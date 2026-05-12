"""
Arbitrage monitor unit tests — verify detection math without hitting real APIs.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from bitcoiners_dca.core.arbitrage import ArbitrageMonitor
from bitcoiners_dca.core.models import FeeSchedule, Ticker
from tests.test_router import FakeExchange


@pytest.mark.asyncio
async def test_no_opportunity_when_spread_small():
    a = FakeExchange("a", ask="350000", bid="349800", taker="0.001")
    b = FakeExchange("b", ask="350200", bid="350000", taker="0.001")
    # Gross spread = (350000 - 350200) / 350200 = -0.06% — negative, no opp

    monitor = ArbitrageMonitor(min_spread_pct=Decimal("1.5"))
    opps = await monitor.detect([a, b])
    assert opps == []


@pytest.mark.asyncio
async def test_detects_real_arbitrage():
    """Gross spread > min threshold AND net profit positive after fees."""
    cheap = FakeExchange("cheap", ask="350000", bid="349800", taker="0.001")
    pricey = FakeExchange("pricey", ask="360000", bid="359000", taker="0.001")
    # Buy cheap@350000, sell pricey@359000 → gross spread = 2.57%
    # Net after 0.1% buy fee + 0.1% sell fee + 0.05% withdraw + 0.3% slippage = ~2.12%

    monitor = ArbitrageMonitor(
        min_spread_pct=Decimal("1.5"),
        slippage_buffer_pct=Decimal("0.3"),
    )
    opps = await monitor.detect([cheap, pricey])

    assert len(opps) == 1
    opp = opps[0]
    assert opp.cheap_exchange == "cheap"
    assert opp.expensive_exchange == "pricey"
    assert opp.spread_pct > Decimal("2.5")
    assert opp.net_profit_pct_after_fees > Decimal("2.0")


@pytest.mark.asyncio
async def test_filters_when_fees_exceed_spread():
    """Spread > threshold but fees eat all profit → not alerted."""
    cheap = FakeExchange("cheap", ask="350000", bid="349800", taker="0.01")   # 1% taker
    pricey = FakeExchange("pricey", ask="354000", bid="353500", taker="0.01")  # 1% taker
    # Gross = (353500 - 350000) / 350000 = 1.0% — at threshold
    # Net = 1.0% - 1% - 1% - 0.05% - 0.3% = NEGATIVE — should not appear

    monitor = ArbitrageMonitor(
        min_spread_pct=Decimal("0.5"),  # below gross to ensure it's not the spread filter
    )
    opps = await monitor.detect([cheap, pricey])
    assert opps == []


@pytest.mark.asyncio
async def test_sorts_by_net_profit():
    """Multiple opportunities are sorted by net profit descending."""
    a = FakeExchange("a", ask="350000", bid="349900", taker="0.001")
    b = FakeExchange("b", ask="355000", bid="354900", taker="0.001")  # ~1.4% spread vs a
    c = FakeExchange("c", ask="360000", bid="359900", taker="0.001")  # ~2.9% spread vs a

    monitor = ArbitrageMonitor(min_spread_pct=Decimal("1.0"))
    opps = await monitor.detect([a, b, c])

    # Expected pairs (buy_cheap, sell_pricey):
    #   a→b, a→c, b→c
    assert len(opps) >= 1
    # First opp should have highest net profit
    for i in range(len(opps) - 1):
        assert opps[i].net_profit_pct_after_fees >= opps[i + 1].net_profit_pct_after_fees
