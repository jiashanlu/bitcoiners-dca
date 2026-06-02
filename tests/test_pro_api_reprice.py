"""
Regression test for _reprice_decision_with_local_fees (audit 2026-06-02
pro-api-payload-drops-per-pair-fees).

The Pro API server prices every hop with one scalar taker (the direct pair's
~0.6% AED fee), overpricing the cheaper BTC/USDT leg (~0.1%) of two-hop
routes. The bot must re-price remote routes with its OWN per-pair fees so a
fee-blind server can't bias the pick toward direct.
"""
from __future__ import annotations

from decimal import Decimal

from bitcoiners_dca.core.models import Ticker
from bitcoiners_dca.core.routing import TradeHop, TradeRoute
from bitcoiners_dca.core.router import (
    RouteCandidate,
    RoutingDecision,
    _ExchangeMarketData,
    _reprice_decision_with_local_fees,
)


class _Ex:
    def __init__(self, name: str):
        self.name = name


def _tk(pair: str, ask: str, bid: str) -> Ticker:
    return Ticker.from_prices(exchange="okx", pair=pair, bid=Decimal(bid), ask=Decimal(ask))


def test_reprice_corrects_usdt_leg_fee():
    ex = _Ex("okx")
    tickers = {
        "BTC/AED": _tk("BTC/AED", "367000", "366900"),
        "USDT/AED": _tk("USDT/AED", "3.67", "3.66"),
        "BTC/USDT": _tk("BTC/USDT", "100000", "99990"),
    }
    # Correct per-pair fees: AED legs 0.6%, BTC/USDT leg 0.1%.
    md = _ExchangeMarketData(
        exchange=ex, tickers=tickers, taker_pct=Decimal("0.006"), balances={},
        taker_pct_by_pair={
            "BTC/AED": Decimal("0.006"),
            "USDT/AED": Decimal("0.006"),
            "BTC/USDT": Decimal("0.001"),
        },
    )

    # Server-priced routes: flat 0.6% on EVERY hop (the bug).
    two_hop = TradeRoute(hops=(
        TradeHop("okx", "USDT/AED", "buy", Decimal("3.67"), Decimal("0.006")),
        TradeHop("okx", "BTC/USDT", "buy", Decimal("100000"), Decimal("0.006")),
    ))
    direct = TradeRoute(hops=(
        TradeHop("okx", "BTC/AED", "buy", Decimal("367000"), Decimal("0.006")),
    ))
    server_two_eff = two_hop.effective_price(Decimal(1000))
    server_direct_eff = direct.effective_price(Decimal(1000))

    decision = RoutingDecision(
        chosen=RouteCandidate(direct, server_direct_eff, server_direct_eff, Decimal(0)),
        alternatives=[RouteCandidate(two_hop, server_two_eff, server_two_eff, Decimal(0))],
    )

    out = _reprice_decision_with_local_fees(decision, [md], Decimal(1000))

    repriced = [out.chosen] + out.alternatives
    two = next(c for c in repriced if len(c.route.hops) == 2)
    one = next(c for c in repriced if len(c.route.hops) == 1)

    # The two-hop USDT leg now uses 0.1%, not 0.6% → strictly cheaper than the
    # server's overpriced figure.
    assert two.effective_price < server_two_eff
    # The direct BTC/AED fee was already correct at 0.6% → unchanged.
    assert one.effective_price == server_direct_eff
    # Re-priced hop fee is the local per-pair value.
    assert two.route.hops[1].taker_pct == Decimal("0.001")
    # Decision is re-sorted ascending by the corrected effective price.
    assert out.chosen.effective_price <= out.alternatives[0].effective_price
