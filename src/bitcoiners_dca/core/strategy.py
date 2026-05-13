"""
DCA strategy engine — decides if it's time to buy, computes the buy amount
(including overlays like buy-the-dip), routes via SmartRouter, executes,
and optionally triggers an auto-withdraw to user's hardware wallet.

Strategy is exchange-agnostic — it receives a list of available Exchanges
and the SmartRouter decides which one to use for each buy.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bitcoiners_dca.core.models import Order, Ticker
from bitcoiners_dca.core.router import RoutingDecision, SmartRouter
from bitcoiners_dca.core.routing import TradeRoute
from bitcoiners_dca.exchanges.base import Exchange, ExchangeError, InsufficientBalanceError


# === Period → per-cycle conversion =========================================
# The dashboard accepts a user-stated spend rate ("AED 1000 / month") and
# the bot needs to translate that into a per-cycle base amount given the
# cron frequency. Deterministic, no calendar drift — 365-day year averages.

_CYCLES_PER_YEAR: dict[str, int] = {
    "hourly": 24 * 365,   # 8760
    "daily": 365,
    "weekly": 52,
    "monthly": 12,
}

_PERIODS_PER_YEAR: dict[str, int] = {
    "daily": 365,
    "weekly": 52,
    "monthly": 12,
    "yearly": 1,
}


def _effective_cycles_per_year(frequency: str, every_n_hours: int = 1) -> int:
    """Effective cycles per year accounting for hourly's `every_n_hours`
    sub-divider. For non-hourly frequencies, every_n_hours is ignored."""
    if frequency == "hourly":
        n = max(1, every_n_hours or 1)
        return _CYCLES_PER_YEAR["hourly"] // n
    return _CYCLES_PER_YEAR[frequency]


def derive_per_cycle(
    budget_amount: Decimal,
    budget_period: str,
    frequency: str,
    every_n_hours: int = 1,
) -> Decimal:
    """Translate a user-stated spend rate into the per-cycle base amount
    the DCA engine uses. `budget_period="cycle"` is a passthrough — the
    entered amount IS the per-cycle amount (legacy/advanced mode).

    `every_n_hours` only matters when frequency=hourly. Defaults to 1
    (every hour). Stretching to N hours raises the per-cycle amount.

    Rounded to 2 decimal places (AED minor-unit precision).
    """
    if budget_period == "cycle":
        return Decimal(budget_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if budget_period not in _PERIODS_PER_YEAR:
        raise ValueError(f"unknown budget_period: {budget_period}")
    if frequency not in _CYCLES_PER_YEAR:
        raise ValueError(f"unknown frequency: {frequency}")
    annual_budget = Decimal(budget_amount) * Decimal(_PERIODS_PER_YEAR[budget_period])
    per_cycle = annual_budget / Decimal(_effective_cycles_per_year(frequency, every_n_hours))
    return per_cycle.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cycles_per_period(frequency: str, period: str, every_n_hours: int = 1) -> Decimal:
    """How many DCA cycles happen per budget period. For the UI preview."""
    if period == "cycle":
        return Decimal(1)
    return Decimal(_effective_cycles_per_year(frequency, every_n_hours)) / Decimal(_PERIODS_PER_YEAR[period])


@dataclass
class StrategyConfig:
    """All knobs the user can tune."""
    base_amount_aed: Decimal
    frequency: str = "weekly"
    pair: str = "BTC/AED"

    # Buy-the-dip overlay
    dip_overlay_enabled: bool = False
    dip_threshold_pct: Decimal = Decimal("-10")
    dip_lookback_days: int = 7
    dip_multiplier: Decimal = Decimal("2.0")

    # Auto-withdraw to hardware wallet at threshold (legacy single-dest).
    # When auto_withdraw_exchanges is non-empty, those per-exchange entries
    # take precedence and the legacy single fields are ignored. The Pydantic
    # config layer in utils.config.AutoWithdrawConfig wires both into here.
    auto_withdraw_enabled: bool = False
    auto_withdraw_address: Optional[str] = None
    auto_withdraw_threshold_btc: Decimal = Decimal("0.01")
    # exchange_name -> {"destination": str, "network": "bitcoin"|"lightning",
    #                   "threshold_btc": Decimal, "enabled": bool}
    auto_withdraw_exchanges: dict = field(default_factory=dict)

    # Execution mode: "taker" | "maker_only" | "maker_fallback"
    execution_mode: str = "taker"
    maker_limit_at: str = "bid"               # "bid" | "midpoint" | "ask_minus_bps"
    maker_spread_bps_below_market: int = 5
    maker_timeout_seconds: int = 600

    # Hard ceiling on per-cycle balance consumption. 0.25 = never spend
    # more than 25% of the chosen exchange's available quote balance in
    # one cycle, regardless of the configured base_amount_aed. Safety net
    # against misconfiguration sweeping a whole wallet.
    max_pct_of_balance: Decimal = Decimal("0.25")


@dataclass
class ExecutionResult:
    """Everything that happened during one DCA cycle.

    For multi-hop routes (e.g. AED→USDT→BTC), `orders` contains every leg
    in execution order. `order` (singular) is preserved as the final
    BTC-receiving order for backward compatibility with existing callers
    (db, dashboard, notifications).
    """
    timestamp: datetime
    intended_amount_aed: Decimal
    overlay_applied: Optional[str]      # e.g. "buy-the-dip 2x"
    routing_decision: Optional[RoutingDecision]
    orders: list[Order] = field(default_factory=list)
    withdrew_btc: Optional[Decimal] = None
    withdrew_to_address: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Distinguishes "the strategy chose not to buy" (overlay skip,
    # maker_only timeout, dip-not-deep-enough) from "the strategy tried
    # to buy and an error blocked it". The scheduler uses this to decide
    # whether to increment consecutive_failures — a 0-order skip that the
    # strategy made on purpose must NOT count as a failure, otherwise
    # time-of-day skips during overnight hours auto-pause the bot after
    # 5 cycles.
    deliberate_skip: bool = False
    # Set when a multi-hop cycle landed in an intermediate currency
    # because hop K-1 succeeded but hop K failed. Lets the scheduler
    # surface a dashboard banner that fires reliably, instead of
    # string-matching error messages for "orphan" (which most paths
    # don't include).
    orphan_amount: Optional[Decimal] = None
    orphan_ccy: Optional[str] = None
    orphan_exchange: Optional[str] = None

    @property
    def order(self) -> Optional[Order]:
        """The final order in the route — typically the BTC-receiving one."""
        return self.orders[-1] if self.orders else None


class DCAStrategy:
    def __init__(
        self,
        config: StrategyConfig,
        router: SmartRouter,
        overlays: Optional[list] = None,
    ):
        self.config = config
        self.router = router
        # When overlays not provided, fall back to the legacy buy-the-dip path
        # driven by StrategyConfig fields. New code should pass overlays.
        self.overlays = overlays or self._legacy_overlays()

    def _legacy_overlays(self) -> list:
        from bitcoiners_dca.strategies import BuyTheDipOverlay
        if self.config.dip_overlay_enabled:
            return [BuyTheDipOverlay(
                threshold_pct=self.config.dip_threshold_pct,
                multiplier=self.config.dip_multiplier,
                lookback_days=self.config.dip_lookback_days,
            )]
        return []

    async def execute(
        self,
        exchanges: list[Exchange],
        historical_price_7d_ago: Optional[Decimal] = None,
        risk_cap_aed: Optional[Decimal] = None,
        market_context: Optional[dict] = None,
    ) -> ExecutionResult:
        """Run one DCA cycle. Returns rich result for logging + notifications."""
        from bitcoiners_dca.strategies import OverlayContext

        result = ExecutionResult(
            timestamp=datetime.utcnow(),
            intended_amount_aed=self.config.base_amount_aed,
            overlay_applied=None,
            routing_decision=None,
        )

        # Pre-cycle sweep: cancel any open BUY orders on the pairs the bot
        # uses, on every connected exchange. Without this, a previous
        # maker_only/maker_fallback run that didn't reach its cancel step
        # (container restart, network blip, retry exhaustion) leaves stale
        # orders that lock up AED and cause every subsequent cycle to fail
        # with "available AED insufficient". Idempotent: 0 open orders is
        # the common case and finishes in milliseconds.
        sweep_pairs = {"BTC/AED", "USDT/AED", "BTC/USDT"}
        for ex in exchanges:
            for pair in sweep_pairs:
                try:
                    canceled = await ex.cancel_all_open_orders(pair)
                    if canceled:
                        result.notes.append(
                            f"pre-cycle: canceled {canceled} stale order(s) "
                            f"on {ex.name} {pair}"
                        )
                except NotImplementedError:
                    pass
                except Exception as e:
                    # Don't fail the cycle just because the sweep had a hiccup.
                    result.notes.append(f"pre-cycle sweep on {ex.name} {pair} skipped: {e}")

        # Apply overlays in config-defined order; multipliers compound.
        amount = self.config.base_amount_aed
        current_price = None
        if self.overlays:
            current_quotes = await self._fetch_current_prices(exchanges)
            if current_quotes:
                current_price = min(current_quotes, key=lambda t: t.ask).ask
            extra = market_context or {}
            ctx = OverlayContext(
                now=datetime.utcnow(),
                base_amount_aed=self.config.base_amount_aed,
                current_price_aed=current_price,
                price_7d_ago_aed=historical_price_7d_ago,
                price_30d_ago_aed=extra.get("price_30d_ago_aed"),
                price_ath_aed=extra.get("price_ath_aed"),
                realized_vol_30d_pct=extra.get("realized_vol_30d_pct"),
                hourly_spread_history=extra.get("hourly_spread_history"),
            )
            applied_notes: list[str] = []
            for overlay in self.overlays:
                ov = overlay.apply(ctx)
                if ov.skip:
                    # Short-circuit: this overlay says skip the cycle entirely.
                    # Mark as deliberate so the scheduler doesn't treat it as
                    # a failure (no order ≠ broken). The 5-consecutive-failure
                    # auto-pause threshold should only fire on REAL errors.
                    result.notes.append(ov.note or f"{overlay.name} skipped cycle")
                    result.deliberate_skip = True
                    return result
                if ov.multiplier != Decimal(1):
                    amount = amount * ov.multiplier
                    if ov.note:
                        applied_notes.append(ov.note)
            if applied_notes:
                result.overlay_applied = " · ".join(applied_notes)

        # Risk-cap clamp (set by the scheduler after consulting RiskManager).
        # We log the cap on the result so post-hoc cycle inspection shows it.
        if risk_cap_aed is not None and amount > risk_cap_aed:
            result.notes.append(
                f"risk-cap clamp: AED {amount} → AED {risk_cap_aed}"
            )
            amount = risk_cap_aed

        result.intended_amount_aed = amount

        # 2. Route to best exchange — balance-aware (skips exchanges that
        # can't fund the intended buy).
        try:
            decision = await self.router.pick(
                exchanges, self.config.pair, required_quote_amount=amount,
            )
            result.routing_decision = decision
            result.notes.append(decision.reason)
        except Exception as e:
            result.errors.append(f"Routing failed: {e}")
            return result

        # Balance clamp: if the user configured a per-cycle amount larger
        # than what the chosen route's quote balance can fund, clamp down
        # to 99% of available so OKX/Binance/etc don't reject with
        # "insufficient AED" on a 15000-AED config + 3700-AED balance.
        # 99% leaves headroom for any maker-rebate / taker-fee accounting
        # the exchange does at order-validation time. Only clamp when the
        # balance is positive AND strictly less than amount — a reported
        # balance of 0 usually means "balance check not supported", not
        # "underfunded" (test stubs default to 0).
        chosen_balance = decision.chosen.quote_balance
        if (
            chosen_balance is not None
            and chosen_balance > 0
            and chosen_balance < amount
        ):
            # Two-layer clamp:
            #   1. Hard ceiling: never spend more than max_pct_of_balance
            #      of available balance in a single cycle (default 25%).
            #      Stops a misconfigured 15000-AED amount from sweeping
            #      a 3700-AED balance to zero on one Buy Now click.
            #   2. Within that ceiling, take 99% of the lesser of (a) the
            #      available balance, (b) the configured amount — leaves
            #      fee headroom so the exchange doesn't reject for being
            #      a hair over.
            max_pct = getattr(self.config, "max_pct_of_balance", Decimal("0.25"))
            try:
                max_pct = Decimal(str(max_pct))
            except Exception:
                max_pct = Decimal("0.25")
            cap_pct_of_balance = (chosen_balance * max_pct).quantize(Decimal("0.01"))
            clamp_ceiling = min(chosen_balance, amount)
            new_amount = min(
                cap_pct_of_balance,
                (clamp_ceiling * Decimal("0.99")).quantize(Decimal("0.01")),
            )
            result.notes.append(
                f"balance clamp: {amount} → {new_amount} "
                f"(max {max_pct * 100:.0f}% of {chosen_balance} "
                f"{decision.chosen.route.input_ccy} on "
                f"{decision.chosen.route.hops[0].exchange})"
            )
            amount = new_amount
            result.intended_amount_aed = amount

        # 3. Execute the route hop-by-hop
        exchange_map = {ex.name: ex for ex in exchanges}
        chosen_route = decision.chosen.route
        # Temp debug — print the chosen route so we can verify each hop's
        # pair + price + input/output currency in the daemon log.
        import logging as _lg
        _lg.getLogger(__name__).info(
            "Chosen route: %d hops, total: %s",
            len(chosen_route.hops), decision.reason,
        )
        for _i, _h in enumerate(chosen_route.hops):
            _lg.getLogger(__name__).info(
                "  hop[%d]: exchange=%s pair=%s in=%s out=%s price=%s",
                _i, _h.exchange, _h.pair,
                getattr(_h, "input_ccy", "?"),
                getattr(_h, "output_ccy", "?"),
                getattr(_h, "price", "?"),
            )
        try:
            orders = await self._execute_route(
                chosen_route, amount, exchange_map, result,
            )
            result.orders = orders
            if orders:
                final = orders[-1]
                result.notes.append(
                    f"Final hop: bought {final.amount_base or '?'} "
                    f"{chosen_route.output_ccy} on {final.exchange}"
                )
        except ExchangeError as e:
            result.errors.append(f"Route execution failed: {e}")
            return result

        # 4. Auto-withdraw: sweep BTC off each configured exchange to the
        # user's self-custody destination(s). Two policy sources:
        #   (a) Per-exchange map `auto_withdraw_exchanges` — preferred. Each
        #       entry is independent: different destination/network/threshold
        #       per exchange, supports Lightning where the exchange does.
        #   (b) Legacy single-destination fields — only used when (a) is
        #       empty AND only fires on the cycle's final exchange.
        # Non-fatal: any failure logs to result.errors and continues.
        if self.config.auto_withdraw_exchanges and self.config.auto_withdraw_enabled:
            for ex in exchanges:
                policy = self.config.auto_withdraw_exchanges.get(ex.name)
                if not policy or not policy.get("enabled") or not policy.get("destination"):
                    continue
                threshold = Decimal(str(policy.get("threshold_btc", "0.001")))
                network = policy.get("network", "bitcoin")
                destination = policy["destination"]
                try:
                    btc_balance = await ex.get_balance("BTC")
                    if not btc_balance or btc_balance.free < threshold:
                        continue
                    fees = await ex.get_fee_schedule(self.config.pair)
                    # On-chain has a withdrawal fee that comes off our balance;
                    # Lightning is effectively zero on OKX so don't subtract.
                    withdraw_fee = (
                        Decimal("0") if network == "lightning"
                        else fees.withdrawal_fee_btc
                    )
                    withdraw_amount = btc_balance.free - withdraw_fee
                    if withdraw_amount <= 0:
                        continue
                    wd = await ex.withdraw_btc(
                        amount_btc=withdraw_amount,
                        address=destination,
                        network=network,
                    )
                    result.withdrew_btc = (result.withdrew_btc or Decimal(0)) + withdraw_amount
                    result.withdrew_to_address = destination
                    result.notes.append(
                        f"Auto-withdrew {withdraw_amount} BTC from {ex.name} "
                        f"via {network} (withdrawal_id={wd.withdrawal_id})"
                    )
                except Exception as e:
                    result.errors.append(f"Auto-withdraw from {ex.name} skipped: {e}")
        elif (
            self.config.auto_withdraw_enabled
            and self.config.auto_withdraw_address
            and chosen_route.output_ccy == "BTC"
            and result.orders
        ):
            # Legacy path: single destination, fires only on final exchange.
            final_ex = exchange_map[result.orders[-1].exchange]
            try:
                btc_balance = await final_ex.get_balance("BTC")
                if btc_balance and btc_balance.free >= self.config.auto_withdraw_threshold_btc:
                    fees = await final_ex.get_fee_schedule(self.config.pair)
                    withdraw_amount = btc_balance.free - fees.withdrawal_fee_btc
                    if withdraw_amount > 0:
                        wd = await final_ex.withdraw_btc(
                            amount_btc=withdraw_amount,
                            address=self.config.auto_withdraw_address,
                        )
                        result.withdrew_btc = withdraw_amount
                        result.withdrew_to_address = self.config.auto_withdraw_address
                        result.notes.append(
                            f"Auto-withdrew {withdraw_amount} BTC from {final_ex.name} "
                            f"(withdrawal_id={wd.withdrawal_id})"
                        )
            except Exception as e:
                # Non-fatal: log but don't fail the whole cycle
                result.errors.append(f"Auto-withdraw skipped: {e}")

        return result

    async def _execute_route(
        self,
        route: TradeRoute,
        input_amount: Decimal,
        exchange_map: dict[str, Exchange],
        result: ExecutionResult,
    ) -> list[Order]:
        """Walk the route hop-by-hop, threading the output of each into the next.

        Each hop respects `config.execution_mode`:
          - taker          : market buy
          - maker_only     : limit buy; skip the cycle if unfilled at timeout
          - maker_fallback : limit buy; if unfilled, cancel + market buy

        If hop K fails after hop K-1 succeeded, we leave the orphan amount in
        whatever account it landed in, surface a clear error, and raise so
        the cycle is marked failed. No auto-retry — manual cleanup.
        """
        if route.cross_exchange:
            raise ExchangeError(
                "Cross-exchange routes are alert-only; not executable."
            )

        orders: list[Order] = []
        current_amount = input_amount
        for i, hop in enumerate(route.hops):
            ex = exchange_map.get(hop.exchange)
            if ex is None:
                raise ExchangeError(
                    f"Route references unknown exchange {hop.exchange!r}"
                )
            try:
                order = await self._execute_hop(ex, hop, current_amount)
            except InsufficientBalanceError:
                if i == 0:
                    raise
                # Hop K-1 succeeded but hop K can't be funded. Record an
                # explicit orphan signal so the dashboard banner reliably
                # fires — don't depend on the error string saying "orphan".
                result.orphan_amount = current_amount
                result.orphan_ccy = hop.input_ccy
                result.orphan_exchange = hop.exchange
                raise ExchangeError(
                    f"Hop {i+1} failed with insufficient balance. "
                    f"Orphaned ~{current_amount} {hop.input_ccy} on "
                    f"{hop.exchange} (output of hop {i} of {len(route.hops)})"
                )
            if order is None:
                # maker_only that didn't fill → cycle skipped, not failed.
                # Flag deliberate_skip so scheduler doesn't count this
                # toward consecutive_failures.
                result.notes.append(
                    f"Hop {i+1}/{len(route.hops)}: maker_only limit timed out, "
                    f"cycle skipped"
                )
                result.deliberate_skip = True
                return orders
            orders.append(order)
            # Defensive: refuse to thread a non-filled amount to the next hop.
            # If hop K-1 didn't actually settle (status != FILLED) or returned
            # a zero/None amount_base, the next hop would compute base = 0 and
            # the exchange precision check rejects with a misleading
            # "below minimum precision" error — that's how we lost cycles
            # before the OKX fill-poll fix.
            from bitcoiners_dca.core.models import OrderStatus as _OS
            if order.status != _OS.FILLED or not order.amount_base or order.amount_base <= 0:
                if i > 0:
                    # Hops 1..N-1 settled into the intermediate currency on
                    # this exchange; record it explicitly so the orphan
                    # banner fires without string-matching the error.
                    result.orphan_amount = current_amount
                    result.orphan_ccy = hop.input_ccy
                    result.orphan_exchange = hop.exchange
                raise ExchangeError(
                    f"Hop {i+1}/{len(route.hops)} on {hop.exchange} {hop.pair} "
                    f"returned status={order.status} amount_base={order.amount_base!r}; "
                    f"refusing to thread to next hop. Funds (~{current_amount} "
                    f"{hop.input_ccy}) may remain on {hop.exchange}."
                )
            current_amount = order.amount_base
            result.notes.append(
                f"Hop {i+1}/{len(route.hops)}: {hop.exchange} {hop.pair} "
                f"({order.type.value}) → {order.amount_base} {hop.output_ccy} "
                f"(filled @ {order.price_filled_avg})"
            )
        return orders

    async def _execute_hop(
        self,
        ex: Exchange,
        hop,
        input_amount: Decimal,
    ) -> Optional[Order]:
        """Execute a single hop, respecting the strategy's execution_mode.

        Returns None when maker_only times out without filling (caller treats
        as a skip, not a failure).
        """
        mode = self.config.execution_mode
        if mode == "taker":
            return await ex.place_market_buy(hop.pair, input_amount)

        # maker_only or maker_fallback: place limit, poll, decide
        from bitcoiners_dca.core.models import OrderStatus
        limit_price = self._compute_limit_price(hop)
        placed = await ex.place_limit_buy(hop.pair, input_amount, limit_price)

        # Short-circuit: if the placed order already reports a terminal status
        # (e.g. dry-run adapters fill immediately; some exchanges fill IOC
        # limits at place-time), skip the poll round-trip.
        if placed.status == OrderStatus.FILLED:
            return placed

        final = await ex.wait_for_fill(
            hop.pair, placed.order_id,
            timeout_seconds=self.config.maker_timeout_seconds,
            poll_interval_seconds=5,
        )
        if final.status == OrderStatus.FILLED:
            return final

        # Not filled — cancel to free the funds
        try:
            await ex.cancel_order(hop.pair, placed.order_id)
        except Exception:
            pass

        if mode == "maker_only":
            return None  # caller treats as skip

        # maker_fallback: market buy at the current ask
        return await ex.place_market_buy(hop.pair, input_amount)

    def _compute_limit_price(self, hop) -> Decimal:
        """Limit price for `hop` based on `maker_limit_at` config.

        We don't re-fetch the ticker here — the hop already carries the ask
        from when the router built the route. For "bid" we approximate as
        ask * (1 - 5bps) since hops don't carry the bid (could be added).
        """
        ask = hop.price
        mode = self.config.maker_limit_at
        if mode == "bid":
            # Approximate bid as ask - 5bps (10bps gives a safer fill but
            # closer to taker pricing). Caller can override.
            return ask * (Decimal(1) - Decimal("0.0005"))
        if mode == "midpoint":
            return ask * (Decimal(1) - Decimal("0.00025"))  # ~half of 5bps
        if mode == "ask_minus_bps":
            bps = Decimal(self.config.maker_spread_bps_below_market)
            return ask * (Decimal(1) - bps / Decimal(10000))
        raise ValueError(f"Unknown maker_limit_at mode: {mode}")

    async def _fetch_current_prices(self, exchanges: list[Exchange]) -> list[Ticker]:
        import asyncio
        tasks = [ex.get_ticker(self.config.pair) for ex in exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if not isinstance(r, Exception)]
