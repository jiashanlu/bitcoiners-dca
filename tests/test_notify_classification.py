"""
`_classify_execution` + `_route_taker_fee_pct` — surface maker/taker
information in Telegram cycle messages.

Exchanges classify fee category by whether the order crossed the spread,
not by `order.type`. A 'limit' order can pay the taker rate if it
crossed; this matters because the existing message just showed the
order type and let the customer guess.

Audit follow-up 2026-05-26.
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
from bitcoiners_dca.core.notifications import (
    _classify_execution,
    _route_taker_fee_pct,
)
from bitcoiners_dca.core.routing import TradeHop, TradeRoute


def _o(
    *,
    type_: OrderType,
    pair: str = "BTC/AED",
    amount_quote: Decimal = Decimal("16.44"),
    fee_base: Decimal = Decimal(0),
    fee_quote: Decimal = Decimal(0),
    price_filled_avg: Decimal | None = Decimal("282000"),
) -> Order:
    return Order(
        exchange="okx",
        order_id="x",
        pair=pair,
        side=OrderSide.BUY,
        type=type_,
        amount_quote=amount_quote,
        amount_base=Decimal("0.0000567"),
        price_filled_avg=price_filled_avg,
        fee_base=fee_base,
        fee_quote=fee_quote,
        status=OrderStatus.FILLED,
        created_at=datetime.now(timezone.utc),
    )


# ─── _classify_execution ───────────────────────────────────────────────


def test_market_order_is_always_taker():
    o = _o(type_=OrderType.MARKET, fee_base=Decimal("0.00000034"))
    assert _classify_execution(o) == "Taker (market)"


def test_market_order_taker_even_when_fee_unknown():
    """A market order's fee category is determined by the order type,
    not by whether fee data came back. Always taker."""
    o = _o(type_=OrderType.MARKET, fee_base=Decimal(0), fee_quote=Decimal(0))
    assert _classify_execution(o) == "Taker (market)"


def test_limit_with_real_maker_fee_on_aed_pair():
    """0.40% effective fee on AED-quoted pair → real passive maker
    fill. (Benbois 2026-05-25 16:00 cycle had this rate.)"""
    # fee_base × price / amount_quote = effective fee %
    # 0.40% × 16.44 AED = 0.0658 AED in fee. /282000 ≈ 0.000000233 BTC
    o = _o(
        type_=OrderType.LIMIT,
        fee_base=Decimal("0.000000233"),
        amount_quote=Decimal("16.44"),
        price_filled_avg=Decimal("282000"),
    )
    assert _classify_execution(o) == "Maker (limit, passive fill)"


def test_limit_with_taker_rate_on_aed_pair_classified_as_crossed_limit():
    """0.60% effective fee on a limit order = the limit crossed the
    spread on placement and was executed as taker. (Benbois 2026-05-25
    22:00 cycle had this rate.)"""
    # 0.6% × 16.44 = 0.0986 AED. /282000 ≈ 0.00000035 BTC
    o = _o(
        type_=OrderType.LIMIT,
        fee_base=Decimal("0.00000035"),
        amount_quote=Decimal("16.44"),
        price_filled_avg=Decimal("282000"),
    )
    assert _classify_execution(o) == "Taker (limit crossed spread)"


def test_limit_with_maker_fee_on_usdt_pair_uses_tighter_threshold():
    """USDT-quoted pairs have a different fee band (maker ~0.08%,
    taker ~0.10%) so the threshold tightens — a 0.05% fill is maker."""
    # 0.05% × 16.44 USDT = 0.00822 USDT fee. We pass it as fee_quote here.
    o = _o(
        type_=OrderType.LIMIT,
        pair="BTC/USDT",
        fee_quote=Decimal("0.00822"),
        amount_quote=Decimal("16.44"),
    )
    assert _classify_execution(o) == "Maker (limit, passive fill)"


def test_limit_with_taker_fee_on_usdt_pair():
    """USDT-quoted pair, 0.10% fee → crossed-spread limit (taker)."""
    o = _o(
        type_=OrderType.LIMIT,
        pair="BTC/USDT",
        fee_quote=Decimal("0.01644"),  # 0.10% of 16.44
        amount_quote=Decimal("16.44"),
    )
    assert _classify_execution(o) == "Taker (limit crossed spread)"


def test_limit_with_zero_fee_returns_limit_fee_unknown():
    """If we can't read the fee at all, fall back to a non-judgmental
    label rather than guessing."""
    o = _o(
        type_=OrderType.LIMIT,
        fee_base=Decimal(0),
        fee_quote=Decimal(0),
    )
    assert _classify_execution(o) == "Limit (fee unknown)"


def test_limit_with_zero_amount_quote_returns_limit_fee_unknown():
    """Division-by-zero guard — degenerate input doesn't crash."""
    o = _o(
        type_=OrderType.LIMIT,
        fee_base=Decimal("0.00000034"),
        amount_quote=Decimal(0),
    )
    assert _classify_execution(o) == "Limit (fee unknown)"


# ─── _route_taker_fee_pct ──────────────────────────────────────────────


def _hop(pair: str, taker: str) -> TradeHop:
    return TradeHop(
        exchange="okx",
        pair=pair,
        side="buy",
        price=Decimal("282000") if pair.endswith("/AED") else Decimal("82000"),
        taker_pct=Decimal(taker),
    )


def test_single_hop_route_returns_hop_fee():
    route = TradeRoute(hops=(_hop("BTC/AED", "0.006"),))
    assert _route_taker_fee_pct(route) == Decimal("0.6")


def test_two_hop_route_compounds_fees():
    """A 0.6% + 0.1% two-hop is NOT exactly 0.7% — fees compound on
    the after-fee output of the previous hop."""
    route = TradeRoute(hops=(
        _hop("USDT/AED", "0.006"),
        _hop("BTC/USDT", "0.001"),
    ))
    # 1 - (1-0.006)(1-0.001) … wait, fees are ADDED, not subtracted:
    # output = input / (price * (1 + fee)) so cost factor is (1+fee).
    # Cumulative cost factor = (1.006)(1.001) = 1.007006
    # → cumulative fee = 0.7006%
    result = _route_taker_fee_pct(route)
    assert abs(result - Decimal("0.7006")) < Decimal("0.0001")


def test_zero_fee_hops_yield_zero_pct():
    route = TradeRoute(hops=(
        _hop("USDT/AED", "0"),
        _hop("BTC/USDT", "0"),
    ))
    assert _route_taker_fee_pct(route) == Decimal(0)


def test_three_hop_route_compounds():
    """3-hop USDC route: 0.6% AED + 0.05% USDC/USDT + 0.1% BTC/USDT."""
    route = TradeRoute(hops=(
        _hop("USDC/AED", "0.006"),
        _hop("USDT/USDC", "0.0005"),
        _hop("BTC/USDT", "0.001"),
    ))
    # (1.006)(1.0005)(1.001) = 1.0075053... → 0.7505...%
    result = _route_taker_fee_pct(route)
    assert abs(result - Decimal("0.7505")) < Decimal("0.001")


# ─── fill-price currency label (stable-funded cycle) ───────────────────


def test_cycle_message_labels_fill_price_in_order_quote_ccy():
    """A USDT-funded cycle fills on BTC/USDT — the price line must read 'USDT',
    not the old hardcoded 'AED' (which mislabelled a USDT price by the FX rate).

    Regression (Ben, 2026-06-08): "@ AED 63135/BTC" was shown for a BTC/USDT
    fill whose price is in USDT.
    """
    from bitcoiners_dca.core.notifications import Notifier
    from bitcoiners_dca.core.strategy import ExecutionResult
    from bitcoiners_dca.utils.config import NotificationsConfig

    order = _o(type_=OrderType.MARKET, pair="BTC/USDT",
               price_filled_avg=Decimal("63135.40"))
    result = ExecutionResult(
        timestamp=datetime.now(timezone.utc),
        intended_amount_aed=Decimal("50"),
        overlay_applied=None,
        routing_decision=None,
        orders=[order],
    )
    msg = Notifier(NotificationsConfig())._format_cycle_message(result)

    assert "USDT 63135.4/BTC" in msg
    bought_line = msg.split("*Bought:*")[1].split("\n")[0]
    assert "AED" not in bought_line
