"""
Multi-hop routing tests — verify the router enumerates and ranks 2-hop routes
and cross-exchange alerts correctly. Uses a MultiPairFakeExchange that lists
multiple tickers so we can exercise the AED→USDT→BTC paths.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from bitcoiners_dca.core.models import Balance, FeeSchedule, Ticker
from bitcoiners_dca.core.router import SmartRouter
from bitcoiners_dca.exchanges.base import Exchange


class MultiPairFakeExchange(Exchange):
    """A stub exchange that quotes multiple pairs.

    Pass a dict like:
        {"BTC/AED": ("ask", "bid"), "USDT/AED": (...), "BTC/USDT": (...)}
    Pairs absent from the dict raise on get_ticker, mirroring real adapters'
    BadSymbol-like behavior.
    """

    def __init__(
        self,
        name: str,
        markets: dict[str, tuple[str, str]],
        taker: str = "0.0015",
        balances: dict[str, str] | None = None,
    ):
        self.name = name
        self._markets = markets
        self._taker = Decimal(taker)
        self._balances = {k: Decimal(v) for k, v in (balances or {}).items()}

    async def health_check(self): return True

    async def get_ticker(self, pair="BTC/AED"):
        if pair not in self._markets:
            raise ValueError(f"{self.name} does not list {pair}")
        ask, bid = self._markets[pair]
        return Ticker.from_prices(
            exchange=self.name, pair=pair,
            bid=Decimal(bid), ask=Decimal(ask),
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

    async def place_market_buy(self, pair, quote_amount): raise NotImplementedError
    async def get_order(self, pair, order_id): raise NotImplementedError
    async def get_trade_history(self, pair="BTC/AED", since=None, limit=100): return []
    async def withdraw_btc(self, amount_btc, address, network="bitcoin"): raise NotImplementedError
    async def get_withdrawal(self, withdrawal_id): raise NotImplementedError


# === Two-hop generation ===

@pytest.mark.asyncio
async def test_two_hop_beats_direct_when_intermediate_is_cheaper():
    """Live-snapshot-like numbers: OKX 2-hop wins by ~0.09% vs direct."""
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED":  ("301050", "300628"),
        "USDT/AED": ("3.665", "3.664"),
        "BTC/USDT": ("81934.6", "81934.5"),
    }, taker="0.0015", balances={"AED": "100000"})

    router = SmartRouter(enable_two_hop=True, intermediates=["USDT"])
    decision = await router.pick([okx], required_quote_amount=Decimal("500"))

    # Chosen route should be the two-hop one (AED→USDT→BTC).
    assert not decision.chosen.route.is_direct
    assert decision.chosen.route.label == "okx: AED→USDT→BTC"
    assert decision.chosen.route.hops[0].pair == "USDT/AED"
    assert decision.chosen.route.hops[1].pair == "BTC/USDT"

    # Alternative should be the direct BTC/AED route on the same exchange.
    assert decision.best_alt.route.is_direct
    assert decision.best_alt.route.label == "okx: BTC/AED"

    # 2-hop should be cheaper effective price.
    assert decision.chosen.effective_price < decision.best_alt.effective_price


@pytest.mark.asyncio
async def test_two_hop_disabled_yields_only_direct():
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED":  ("301050", "300628"),
        "USDT/AED": ("3.665", "3.664"),
        "BTC/USDT": ("81934.6", "81934.5"),
    }, balances={"AED": "100000"})

    router = SmartRouter(enable_two_hop=False)
    decision = await router.pick([okx], required_quote_amount=Decimal("500"))
    assert decision.chosen.route.is_direct


@pytest.mark.asyncio
async def test_two_hop_skipped_when_intermediate_pair_absent():
    """Exchange that lists BTC/AED but NOT USDT/AED should only yield direct."""
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED": ("301050", "300628"),
    }, balances={"AED": "100000"})

    router = SmartRouter(enable_two_hop=True, intermediates=["USDT"])
    decision = await router.pick([okx], required_quote_amount=Decimal("500"))
    assert decision.chosen.route.is_direct
    assert decision.alternatives == []  # no two-hop candidate


@pytest.mark.asyncio
async def test_three_hop_emitted_when_legs_present():
    """BitOasis-shaped case: AED→USDC→USDT→BTC chained on one venue.

    Confirms 3-hop enumeration produces a candidate when all three legs
    are listed (and skipping any leg suppresses it). The chosen route is
    whichever ranks cheapest after fees — the assertion here is only
    that 3-hop participates in the candidate set, not that it wins.
    """
    bo = MultiPairFakeExchange("bitoasis", markets={
        "BTC/AED":   ("301050", "300628"),
        "USDT/AED":  ("3.665", "3.664"),
        "USDC/AED":  ("3.665", "3.664"),
        "USDT/USDC": ("1.001", "0.999"),
        "BTC/USDT":  ("81934.6", "81934.5"),
    }, taker="0.0015", balances={"AED": "100000"})

    router = SmartRouter(enable_two_hop=True, intermediates=["USDT", "USDC"])
    decision = await router.pick([bo], required_quote_amount=Decimal("500"))

    all_routes = [decision.chosen] + decision.alternatives
    three_hop = [r for r in all_routes if len(r.route.hops) == 3]
    assert len(three_hop) >= 1, "expected ≥1 three-hop candidate, got none"
    assert "AED→USDC→USDT→BTC" in three_hop[0].route.label \
        or "AED→USDT→USDC→BTC" in three_hop[0].route.label


@pytest.mark.asyncio
async def test_three_hop_skipped_when_middle_leg_missing():
    """No USDT/USDC pair → no 3-hop candidate even though USDC/AED exists."""
    bo = MultiPairFakeExchange("bitoasis", markets={
        "BTC/AED":  ("301050", "300628"),
        "USDT/AED": ("3.665", "3.664"),
        "USDC/AED": ("3.665", "3.664"),
        "BTC/USDT": ("81934.6", "81934.5"),
        # No USDT/USDC and no BTC/USDC → 3-hop has no valid chain.
    }, taker="0.0015", balances={"AED": "100000"})

    router = SmartRouter(enable_two_hop=True, intermediates=["USDT", "USDC"])
    decision = await router.pick([bo], required_quote_amount=Decimal("500"))
    all_routes = [decision.chosen] + decision.alternatives
    assert not any(len(r.route.hops) == 3 for r in all_routes)


@pytest.mark.asyncio
async def test_router_compares_across_exchanges_and_intra_exchange_2hop():
    """OKX has 2-hop. BitOasis only has direct (with worse fees).
    Best route should be OKX 2-hop."""
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED":  ("301050", "300628"),
        "USDT/AED": ("3.665", "3.664"),
        "BTC/USDT": ("81934.6", "81934.5"),
    }, taker="0.0015", balances={"AED": "100000"})
    bo = MultiPairFakeExchange("bitoasis", markets={
        "BTC/AED": ("300884", "300825"),
    }, taker="0.005", balances={"AED": "100000"})

    router = SmartRouter(enable_two_hop=True, intermediates=["USDT"])
    decision = await router.pick([okx, bo], required_quote_amount=Decimal("500"))

    assert decision.chosen.route.label == "okx: AED→USDT→BTC"


# === Cross-exchange alerts ===

@pytest.mark.asyncio
async def test_cross_exchange_alert_emitted_above_min_size():
    """Big-size cycle: cross-exchange route surfaces as alert alongside an
    executable direct route."""
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED":  ("301050", "300628"),     # gives executable direct route
        "USDT/AED": ("3.665", "3.664"),
    }, taker="0.0015", balances={"AED": "100000"})
    bn = MultiPairFakeExchange("binance", markets={
        "BTC/USDT": ("81940", "81939"),
    }, taker="0.001", balances={"USDT": "0"})

    router = SmartRouter(
        enable_two_hop=True,
        enable_cross_exchange_alerts=True,
        cross_exchange_min_size_aed=Decimal("25000"),
        cross_exchange_withdrawal_costs={"USDT": Decimal("1.5")},
    )
    decision = await router.pick([okx, bn], required_quote_amount=Decimal("25000"))

    assert decision.cross_exchange_alerts, "expected at least one cross alert"
    alert = decision.cross_exchange_alerts[0]
    assert alert.route.cross_exchange
    assert alert.route.hops[0].exchange == "okx"
    assert alert.route.hops[1].exchange == "binance"


@pytest.mark.asyncio
async def test_cross_exchange_alert_suppressed_below_min_size():
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED":  ("301050", "300628"),
        "USDT/AED": ("3.665", "3.664"),
    }, balances={"AED": "1000"})
    bn = MultiPairFakeExchange("binance", markets={
        "BTC/USDT": ("81940", "81939"),
    }, balances={"USDT": "0"})

    router = SmartRouter(
        enable_two_hop=True,
        enable_cross_exchange_alerts=True,
        cross_exchange_min_size_aed=Decimal("25000"),
    )
    decision = await router.pick([okx, bn], required_quote_amount=Decimal("500"))
    assert decision.cross_exchange_alerts == []


# === Intermediate-direct unit conversion (audit 2026-06-02 / task #212) ===

@pytest.mark.asyncio
async def test_intermediate_direct_balance_converted_to_aed_equivalent():
    """An idle USDT balance funds a BTC/USDT route. The router must compare
    its AED-equivalent value (not the raw USDT number) against the AED cycle
    size, and carry the AED→USDT rate for execution sizing.

    Regression: previously quote_balance was the raw USDT figure, so 300 USDT
    (~1100 AED of buying power) was tested as `300 < 1000` AED and the route
    was silently dropped — and if it won, a 1000-AED cycle placed a 1000-USDT
    (~3670 AED) order.
    """
    usdt_ask = Decimal("3.67")
    okx = MultiPairFakeExchange("okx", markets={
        "BTC/AED":  ("367000", "366900"),
        "USDT/AED": (str(usdt_ask), "3.66"),
        "BTC/USDT": ("100000", "99990"),
    }, balances={"AED": "5000", "USDT": "300"})

    router = SmartRouter(
        enable_two_hop=True,
        intermediates=["USDT"],
        prefer_intermediate_balance=True,
        prefer_intermediate_min=Decimal("10"),
    )
    decision = await router.pick([okx], required_quote_amount=Decimal("1000"))

    all_candidates = [decision.chosen] + decision.alternatives
    inter_direct = [
        c for c in all_candidates
        if c.route.is_direct and c.route.hops[0].pair == "BTC/USDT"
    ]
    assert inter_direct, "intermediate-direct BTC/USDT route was not emitted"
    c = inter_direct[0]

    # quote_balance is the AED-equivalent (300 USDT * 3.67), not raw 300.
    assert c.quote_balance == Decimal("300") * usdt_ask
    # Carries the AED→USDT rate so the strategy sizes the order in USDT.
    assert c.route.quote_to_input_rate == Decimal(1) / usdt_ask
    # 1100 AED-equiv >= 1000 AED cycle → NOT dropped by the balance filter.
    excluded_pairs = [e.route.hops[0].pair for e in decision.excluded]
    assert "BTC/USDT" not in excluded_pairs
    # Prefer-stablecoin nudge is carried as a multiplier (applied, not dead).
    assert c.score_multiplier < Decimal(1)
