"""
DCA strategy engine — decides if it's time to buy, computes the buy amount
(including overlays like buy-the-dip), routes via SmartRouter, executes.

Strategy is exchange-agnostic — it receives a list of available Exchanges
and the SmartRouter decides which one to use for each buy.

Auto-withdraw is retired from the product surface — see
feedback-kill-auto-withdraw-until-lightning. The strategy honors a
kill-switch when the legacy config fields are still set.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

# Module-level logger so the on-chain-fetch error path (and any future
# bare logger.exception() call) has a real logger to write to. Before
# this import the `except Exception: logger.exception(...)` block fell
# back to NameError and masked the underlying issue. Audit P1 2026-05-21.
logger = logging.getLogger(__name__)

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


# Clean divisors of 24 — the only cadences a `*/N` cron expresses without
# drifting across the day boundary.
_CLEAN_HOUR_DIVISORS = (24, 12, 8, 6, 4, 3, 2, 1)


def snap_every_n_hours(n) -> int:
    """Snap `every_n_hours` to the closest clean divisor of 24 at or below n.

    SINGLE source of truth, shared by the scheduler's cron builder AND the
    budget→per-cycle derivation. Before this, the cron builder snapped a
    non-divisor (e.g. 5 → fires every 4h) while derive_per_cycle sized the
    amount for the RAW cadence (a 5h-sized buy every 4h = ~25% budget
    overspend on every cycle, audit 2026-06-10 P1).
    """
    try:
        n = int(n or 1)
    except (TypeError, ValueError):
        n = 1
    n = min(24, max(1, n))
    return next(d for d in _CLEAN_HOUR_DIVISORS if d <= n)


def _effective_cycles_per_year(frequency: str, every_n_hours: int = 1) -> int:
    """Effective cycles per year accounting for hourly's `every_n_hours`
    sub-divider. For non-hourly frequencies, every_n_hours is ignored."""
    if frequency == "hourly":
        # Snapped, NOT raw — must match the cadence the cron actually fires.
        return _CYCLES_PER_YEAR["hourly"] // snap_every_n_hours(every_n_hours)
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
    # Decimal so sub-bp precision works (e.g. 0.2 ≈ "at the touch"). Set
    # 0.2–1 if you want fast fills; 5+ for max maker rebate at the cost
    # of frequent timeouts that fall back to taker anyway.
    maker_spread_bps_below_market: Decimal = Decimal("1")
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
        db=None,
    ):
        self.config = config
        self.router = router
        # When overlays not provided, fall back to the legacy buy-the-dip path
        # driven by StrategyConfig fields. New code should pass overlays.
        self.overlays = overlays or self._legacy_overlays()
        # Optional: Database for auto-withdraw idempotency. When provided,
        # strategy persists every successful withdrawal and checks for a
        # recent (last 60 min) withdrawal before initiating a new one — so
        # a crash mid-withdraw can't trigger a duplicate on the next cycle.
        # Tests don't pass db; behaviour is unchanged when None.
        self.db = db

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
            timestamp=datetime.now(timezone.utc),
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
            onchain_signals = await self._maybe_fetch_onchain_signals(extra)
            ctx = OverlayContext(
                now=datetime.now(timezone.utc),
                base_amount_aed=self.config.base_amount_aed,
                current_price_aed=current_price,
                price_7d_ago_aed=historical_price_7d_ago,
                price_30d_ago_aed=extra.get("price_30d_ago_aed"),
                price_ath_aed=extra.get("price_ath_aed"),
                realized_vol_30d_pct=extra.get("realized_vol_30d_pct"),
                hourly_spread_history=extra.get("hourly_spread_history"),
                onchain_signals=onchain_signals,
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
        # can't fund the intended buy). Passes the license token through
        # so the router can try the hosted Pro API first when configured
        # (see workspace/bitcoiners-pro-api-plan.md). License is optional;
        # Free-tier, self-host, and test fixtures that don't have a
        # `.license` section pass None and stay on local logic.
        # Double getattr because StrategyConfig (used in unit tests) has
        # no `.license` attribute at all — direct access raises.
        _lic_section = getattr(self.config, "license", None)
        license_token = getattr(_lic_section, "key", None)
        try:
            decision = await self.router.pick(
                exchanges,
                self.config.pair,
                required_quote_amount=amount,
                license_token=license_token,
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
        # Chosen-route trace lives at DEBUG so it's available when an
        # operator turns up log_level but doesn't spam INFO + tenant
        # logs every cycle. Audit B-#18 2026-05-21 (was logger.info,
        # marked as "Temp debug").
        logger.debug(
            "Chosen route: %d hops, total: %s",
            len(chosen_route.hops), decision.reason,
        )
        for _i, _h in enumerate(chosen_route.hops):
            logger.debug(
                "  hop[%d]: exchange=%s pair=%s in=%s out=%s price=%s",
                _i, _h.exchange, _h.pair,
                getattr(_h, "input_ccy", "?"),
                getattr(_h, "output_ccy", "?"),
                getattr(_h, "price", "?"),
            )
        # Convert the AED budget into the route's INPUT currency before
        # execution. Intermediate-direct routes spend an idle stablecoin
        # (input=USDT/USDC), so a 1000-AED budget must become ~272 USDT, not
        # 1000 USDT (~3.67x over-spend). Direct AED routes carry rate=None →
        # no conversion. (Audit 2026-06-02 task #212.)
        exec_amount = amount
        _q2i = getattr(chosen_route, "quote_to_input_rate", None)
        if _q2i is not None and _q2i > 0:
            exec_amount = amount * _q2i
            result.notes.append(
                f"input-ccy conversion: AED {amount} → "
                f"{exec_amount} {chosen_route.input_ccy} "
                f"(rate {_q2i})"
            )
        elif chosen_route.input_ccy != self.config.pair.split("/")[1]:
            # Last line of defense (audit 2026-06-10 P0): a route that spends
            # a different currency than the cycle quote MUST carry a
            # conversion rate — the router/decoder guarantee it. Executing
            # the raw quote-denominated budget as input currency would
            # overspend by the FX rate (~3.67x for USDT), bypassing every
            # risk cap. Refuse the cycle rather than guess.
            result.errors.append(
                f"refusing route {decision.chosen.label}: input currency "
                f"{chosen_route.input_ccy} differs from cycle quote "
                f"{self.config.pair.split('/')[1]} but the route carries no "
                f"conversion rate — executing would overspend by the FX rate"
            )
            return result
        try:
            orders = await self._execute_route(
                chosen_route, exec_amount, exchange_map, result,
            )
            result.orders = orders
            if orders:
                # Stamp the cycle's AED spend on the SPENDING leg (hop 1)
                # only. For AED-quoted hops this equals amount_quote; for a
                # stable-funded hop it is the AED budget the stablecoin spend
                # represents. Hop 2+ stays None — counting them too would
                # double-count multi-hop cycles. The daily cap and AED totals
                # read this column (audit 2026-06-10 P1: stable-funded cycles
                # were invisible to max_daily_aed).
                orders[0].amount_quote_aed = amount
                final = orders[-1]
                result.notes.append(
                    f"Final hop: bought {final.amount_base or '?'} "
                    f"{chosen_route.output_ccy} on {final.exchange}"
                )
        except ExchangeError as e:
            result.errors.append(f"Route execution failed: {e}")
            return result

        # 4. Auto-withdraw: parked until Lightning withdraw lands as a Pro
        # feature. On-chain-only auto-withdraw burns ~0.0002 BTC (~$15-20)
        # per sweep, which eats AED 49 customer savings. Manual withdraw
        # via the /withdrawals dashboard page is the supported flow.
        # The exchange withdraw_btc() adapters, DB schema, address book,
        # and SecretStore stay in place as plumbing for re-enablement.
        # See feedback-kill-auto-withdraw-until-lightning in memory.
        if self.config.auto_withdraw_enabled:
            result.notes.append(
                "Auto-withdraw is disabled at the product level. Use the "
                "dashboard /withdrawals page to withdraw manually."
            )

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
            is_final_hop = (i == len(route.hops) - 1)
            partial_with_btc = (
                order.status == _OS.PARTIAL
                and order.amount_base
                and order.amount_base > 0
            )
            # A PARTIAL on the FINAL hop is acceptable — we keep the
            # filled portion as the cycle's order. Threading to a next
            # hop with a partial amount isn't safe (intermediate hop
            # would compute base from a fraction of the expected input),
            # so reject PARTIAL on hops < final.
            if (
                (order.status != _OS.FILLED and not (is_final_hop and partial_with_btc))
                or not order.amount_base
                or order.amount_base <= 0
            ):
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
            # Thread the NET amount to the next hop. Some exchanges (OKX
            # spot buys) bill the fee in the RECEIVED asset — the wallet
            # actually holds amount_base − fee_base, so spending the gross
            # fill on hop K+1 overdraws by the fee: the exchange either
            # rejects with insufficient-funds or silently dips into any
            # pre-existing balance of that asset (audit 2026-06-10 P1).
            current_amount = order.amount_base
            if order.fee_base and order.fee_base > 0:
                current_amount = current_amount - order.fee_base
                if current_amount <= 0:
                    raise ExchangeError(
                        f"Hop {i+1}/{len(route.hops)} fee ({order.fee_base}) "
                        f"consumed the entire fill ({order.amount_base}) — "
                        f"refusing to thread a non-positive amount"
                    )
            result.notes.append(
                f"Hop {i+1}/{len(route.hops)}: {hop.exchange} {hop.pair} "
                f"({order.type.value}) → {order.amount_base} {hop.output_ccy} "
                f"(filled @ {order.price_filled_avg}"
                + (f", fee {order.fee_base} {hop.output_ccy} deducted before next hop"
                   if order.fee_base and order.fee_base > 0 else "")
                + ")"
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

        # Not fully filled at timeout. Cancel the resting remainder, then
        # decide off the AUTHORITATIVE post-cancel state — never the stale
        # `final` poll snapshot. Between the last poll and the cancel landing
        # the limit may have filled (partially or fully); deciding off `final`
        # treated those as unfilled and re-bought the FULL amount on top,
        # spending ~1.6-2x the intended AED (audit 2026-06-02: P0
        # maker-fallback-partial-double-buy + P1 cancel-fill-race). Each
        # adapter's cancel_order returns get_order() internally, so its result
        # is the real final state; cancelling an already-filled order raises,
        # in which case we re-fetch rather than assume it's safe to buy again.
        settled = final
        confirmed_dead = False
        try:
            cancelled = await ex.cancel_order(hop.pair, placed.order_id)
            if cancelled is not None:
                settled = cancelled
                confirmed_dead = True
        except Exception:
            try:
                refetched = await ex.get_order(hop.pair, placed.order_id)
                if refetched is not None:
                    settled = refetched
            except Exception:
                pass

        filled_base = settled.amount_base or Decimal(0)

        # Filled in full during the cancel window — use that fill, never
        # market-buy on top of it.
        if settled.status == OrderStatus.FILLED:
            return settled

        # Partial fill — keep exactly what we bought, recorded as PARTIAL so
        # cost basis reflects the real BTC. Do NOT top up via market_buy (even
        # in maker_fallback): that double-counts the filled portion and puts
        # two orders behind one hop, which route execution doesn't model. The
        # next cycle catches up.
        if filled_base > 0:
            return settled.model_copy(update={"status": OrderStatus.PARTIAL})

        if mode == "maker_only":
            return None  # caller treats as skip

        # maker_fallback, nothing filled. Only fall back to a market buy once
        # we have an authoritative read that the limit is truly dead
        # (cancelled/rejected). If the state is unknown or still pending, a
        # market buy risks doubling the spend if that limit later fills — skip
        # this cycle instead; the next cycle retries cleanly.
        if confirmed_dead and settled.status in (
            OrderStatus.CANCELLED, OrderStatus.REJECTED
        ):
            return await ex.place_market_buy(hop.pair, input_amount)

        logger.warning(
            "maker_fallback: could not confirm limit %s on %s is dead and "
            "unfilled (status=%s) — skipping market fallback this cycle to "
            "avoid a possible double-buy; next cycle retries.",
            placed.order_id, hop.pair, settled.status,
        )
        return None

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

    async def _maybe_fetch_onchain_signals(self, extra: dict) -> Optional[dict]:
        # Skip the network call unless an overlay actually needs it. Look
        # for any overlay class whose name starts with "onchain_" — keeps
        # the strategy decoupled from the specific overlay registry.
        wants_onchain = any(
            getattr(ov, "name", "").startswith("onchain_") for ov in self.overlays
        )
        if not wants_onchain:
            return None
        # Caller may pre-supply signals (back-tests, tests) — respect those.
        supplied = extra.get("onchain_signals")
        if supplied is not None:
            return supplied
        try:
            from bitcoiners_dca.core.onchain import (
                get_default_client, OnchainSignalError, SUPPORTED_METRICS,
            )
            client = get_default_client()
            wanted = {getattr(ov, "metric", None) for ov in self.overlays}
            wanted.discard(None)
            wanted = {m for m in wanted if m in SUPPORTED_METRICS}
            signals: dict = {}
            for metric in wanted:
                try:
                    signals[metric] = await client.get(metric)
                except OnchainSignalError:
                    # Bot must keep DCA'ing even if BRK is unreachable.
                    pass
            return signals or None
        except Exception:
            logger.exception("on-chain signal fetch failed unexpectedly")
            return None
