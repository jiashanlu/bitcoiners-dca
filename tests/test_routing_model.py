"""
Tests for the TradeRoute / TradeHop pure model. No I/O; pure math.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from bitcoiners_dca.core.routing import TradeHop, TradeRoute


def _buy_hop(exchange="okx", pair="BTC/AED", price="300000", taker="0.0015"):
    return TradeHop(
        exchange=exchange, pair=pair, side="buy",
        price=Decimal(price), taker_pct=Decimal(taker),
    )


# === Hop ===

def test_hop_expected_output_buy_applies_taker_fee():
    """1000 AED at 300k AED/BTC with 0.15% taker → 1000 / (300000*1.0015) BTC."""
    hop = _buy_hop()
    out = hop.expected_output(Decimal(1000))
    expected = Decimal(1000) / (Decimal("300000") * Decimal("1.0015"))
    assert out == expected


def test_hop_input_output_currencies():
    hop = _buy_hop(pair="BTC/AED")
    assert hop.input_ccy == "AED"
    assert hop.output_ccy == "BTC"
    assert hop.base_ccy == "BTC"
    assert hop.quote_ccy == "AED"


# === Route ===

def test_single_hop_route_matches_hop_math():
    hop = _buy_hop()
    route = TradeRoute(hops=(hop,))
    assert route.is_direct
    assert route.expected_output(Decimal(1000)) == hop.expected_output(Decimal(1000))
    assert route.input_ccy == "AED"
    assert route.output_ccy == "BTC"
    assert route.label == "okx: BTC/AED"


def test_two_hop_chains_outputs_into_next_input():
    """1000 AED → USDT (at 3.67) → BTC (at 82000) on OKX, with 0.15% taker each."""
    h1 = TradeHop("okx", "USDT/AED", "buy", Decimal("3.67"), Decimal("0.0015"))
    h2 = TradeHop("okx", "BTC/USDT", "buy", Decimal("82000"), Decimal("0.0015"))
    route = TradeRoute(hops=(h1, h2))

    expected_usdt = Decimal(1000) / (Decimal("3.67") * Decimal("1.0015"))
    expected_btc = expected_usdt / (Decimal("82000") * Decimal("1.0015"))
    assert route.expected_output(Decimal(1000)) == expected_btc
    assert route.input_ccy == "AED"
    assert route.output_ccy == "BTC"
    assert not route.is_direct
    assert route.label == "okx: AED→USDT→BTC"


def test_route_rejects_broken_chain():
    """Hop 1 outputs USDT but hop 2 expects EUR — must fail at construction."""
    h1 = TradeHop("okx", "USDT/AED", "buy", Decimal("3.67"), Decimal("0.0015"))
    h2 = TradeHop("okx", "BTC/EUR", "buy", Decimal("75000"), Decimal("0.0015"))
    with pytest.raises(ValueError, match="don't chain"):
        TradeRoute(hops=(h1, h2))


def test_route_rejects_empty():
    with pytest.raises(ValueError, match="at least one hop"):
        TradeRoute(hops=())


def test_cross_exchange_route_with_fixed_costs():
    """OKX AED→USDT then withdraw (cost 1.5 USDT) then Binance USDT→BTC."""
    h1 = TradeHop("okx", "USDT/AED", "buy", Decimal("3.67"), Decimal("0.0015"))
    h2 = TradeHop("binance", "BTC/USDT", "buy", Decimal("82000"), Decimal("0.001"))
    # 1.5 USDT withdrawal fee, expressed in the INPUT ccy (AED) via spot rate
    # For testing we use AED-denominated fixed_costs directly.
    route = TradeRoute(
        hops=(h1, h2),
        cross_exchange=True,
        fixed_costs=Decimal("5.5"),   # 1.5 USDT ≈ 5.5 AED
    )
    assert route.cross_exchange
    assert route.exchanges_involved == ("okx", "binance")
    out = route.expected_output(Decimal(1000))
    # Direct check: should be less than the same route without fixed_costs
    no_cost = TradeRoute(hops=(h1, h2))
    assert out < no_cost.expected_output(Decimal(1000))
    assert route.label.startswith("cross: ")


def test_effective_price_scales_inversely_with_output():
    """A route that yields more output per input has a lower effective_price."""
    cheap = _buy_hop(price="300000")
    pricey = _buy_hop(price="310000")
    r_cheap = TradeRoute(hops=(cheap,))
    r_pricey = TradeRoute(hops=(pricey,))
    assert r_cheap.effective_price() < r_pricey.effective_price()


def test_fixed_costs_above_input_yields_zero():
    h = _buy_hop()
    route = TradeRoute(hops=(h,), fixed_costs=Decimal(2000))
    # Trying to spend 1000 AED with 2000 AED fixed cost → can't proceed
    assert route.expected_output(Decimal(1000)) == Decimal(0)
