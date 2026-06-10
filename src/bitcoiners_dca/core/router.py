"""
Smart router — enumerates candidate `TradeRoute`s (direct + same-exchange
two-hop + cross-exchange) and picks the best one for a DCA cycle.

Route ranking metric is `effective_price` net of taker fees on every hop,
including any fixed costs (e.g. inter-exchange withdrawal fees).

Filters applied, in order:
  1. Spread filter — drop routes whose hops have spreads above the threshold
  2. Balance filter — drop routes the user can't fund (when required_amount given)
  3. Preferred-exchange bonus — small discount when the user pins a venue

Cross-exchange routes are NEVER returned as the `chosen` route. They surface
as `cross_exchange_alerts` for Telegram notification so the user can manually
execute when the math is meaningfully positive at their cycle size. Cross-
exchange auto-execution is out of scope because transit time creates price
risk and orphaned-state cleanup is brittle.

See `docs/ROUTING.md` for the math and live-snapshot comparisons.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from bitcoiners_dca.core.models import Ticker, FeeSchedule, OrderMinimum
from bitcoiners_dca.core.routing import TradeHop, TradeRoute
from bitcoiners_dca.exchanges.base import Exchange

logger = logging.getLogger(__name__)

# Pro API feature flag. When set, the router tries the hosted /api/pro/route
# endpoint first and falls back to local logic on any failure (timeout, 4xx,
# 5xx, or stub:true). Unset = unchanged behavior (local-only). See
# workspace/bitcoiners-pro-api-plan.md for the migration plan.
_PRO_API_URL = os.environ.get("BITCOINERS_DCA_PRO_API_URL", "").rstrip("/")
_PRO_API_TIMEOUT_SECONDS = float(
    os.environ.get("BITCOINERS_DCA_PRO_API_TIMEOUT", "5")
)


@dataclass
class RouteCandidate:
    """A scored route under consideration for execution."""
    route: TradeRoute
    effective_price: Decimal     # input ccy per unit of output ccy, after fees
    score: Decimal               # ranking metric; preference bonus applied here
    max_spread_pct: Decimal      # worst spread across all hops (for filtering)
    # Multiplicative preference applied to `score` ONCE in _apply_filters
    # (e.g. the "prefer idle stablecoin" nudge). Kept separate from `score`
    # because the final scoring pass reassigns `score` from effective_price,
    # which silently discarded a directly-mutated score (audit 2026-06-02).
    score_multiplier: Decimal = Decimal(1)
    quote_balance: Optional[Decimal] = None
    # Minimum amount of route input currency this route can execute, derived
    # from each hop's partner-published OrderMinimum. 0 = no known floor.
    min_input_amount: Decimal = Decimal(0)
    # Per-hop minimums kept for the UI to render "BitOasis min: 0.000048 BTC"
    # without re-fetching.
    hop_minimums: tuple[Optional[OrderMinimum], ...] = ()
    note: str = ""

    @property
    def label(self) -> str:
        return self.route.label

    @property
    def is_cross_exchange(self) -> bool:
        return self.route.cross_exchange


@dataclass
class ExcludedRoute:
    """A route that survived enumeration but was filtered out before scoring.

    Surfaced in `RoutingDecision.excluded` so the dashboard can tell the
    user *why* a venue didn't appear in the picks ("BitOasis min AED 14
    at current BTC price — your cycle is AED 10").
    """
    route: TradeRoute
    reason: str
    min_input_amount: Decimal = Decimal(0)
    hop_minimums: tuple[Optional[OrderMinimum], ...] = ()


@dataclass
class RoutingDecision:
    chosen: RouteCandidate
    alternatives: list[RouteCandidate]
    cross_exchange_alerts: list[RouteCandidate] = field(default_factory=list)
    # Routes the user can't take with the given cycle size. Each entry
    # carries the reason — typically "below partner minimum". UI uses
    # this to render an honest disclaimer.
    excluded: list[ExcludedRoute] = field(default_factory=list)
    reason: str = ""

    @property
    def best_alt(self) -> Optional[RouteCandidate]:
        return self.alternatives[0] if self.alternatives else None

    def price_premium_vs_alt_pct(self) -> Decimal:
        """How much MORE the next-best alternative would have cost.

        Valid because every candidate's `effective_price` is normalised to
        the cycle's quote currency by `_effective_price_in_quote` before
        scoring — see that helper. Without normalisation this compared a
        USDT-denominated price against an AED one and returned the FX rate
        as a bogus "saving" (e.g. "Saved 269%" for a BTC/USDT vs BTC/AED).
        """
        if not self.best_alt:
            return Decimal(0)
        diff = self.best_alt.effective_price - self.chosen.effective_price
        return (diff / self.chosen.effective_price) * Decimal(100)


def _effective_price_in_quote(route: TradeRoute, sample_amount: Decimal) -> Decimal:
    """Route effective price in the cycle's QUOTE currency (e.g. AED) per unit
    of target asset, after fees — the unit every candidate must share to be
    ranked or compared against each other.

    `TradeRoute.effective_price()` returns the price in the route's INPUT
    currency. For most routes input == quote (AED-direct, AED→USDT→BTC) so the
    two coincide. But an 'intermediate-direct' route funded from idle USDT has
    input=USDT, so its native price is in USDT/BTC — numerically ~3.67x smaller
    than an AED/BTC price. Ranking or comparing those side-by-side silently
    mixed currencies: a USDT route always 'won' on magnitude, and the savings
    line surfaced the USDT→AED FX rate as a fake premium. `quote_to_input_rate`
    (input-per-quote, the same field the #212 balance fix uses) converts back.
    """
    eff = route.effective_price(sample_amount)
    rate = route.quote_to_input_rate
    if rate and rate > 0:
        eff = eff / rate
    return eff


# === Quote bundle, fetched once per exchange to avoid duplicate ticker hits. ===

@dataclass
class _ExchangeMarketData:
    exchange: Exchange
    tickers: dict[str, Ticker]               # pair -> ticker (failed pairs absent)
    taker_pct: Decimal                       # default / fallback fee (e.g. exchange's USDT-pair rate)
    balances: dict[str, Decimal]             # asset -> free balance (0 if absent)
    minimums: dict[str, OrderMinimum] = field(default_factory=dict)  # pair -> min
    # Per-pair taker fee, populated for AED-quoted pairs where OKX
    # charges substantially more (~0.6%) than its standard USDT-pair
    # taker (~0.1%). Without this, the router systematically under-
    # prices the AED leg of multi-hop routes and over-prefers direct.
    # Audit follow-up 2026-05-24 from Ben's "why 0.6%?" question.
    taker_pct_by_pair: dict[str, Decimal] = field(default_factory=dict)

    def taker_for(self, pair: str) -> Decimal:
        """Return the taker fee for a specific pair, falling back to
        the exchange's default if no per-pair value was fetched."""
        return self.taker_pct_by_pair.get(pair, self.taker_pct)


# === The router. ===

class SmartRouter:
    """Picks the cheapest viable `TradeRoute` from available exchanges.

    Args:
        exclude_if_spread_pct_above: drop routes whose hops have spreads
            wider than this percent (signals thin orderbook).
        preferred_exchange: name of an exchange to favor in ties.
        preferred_bonus_pct: how much to discount the preferred exchange's
            score (in %). 0.5 = treat preferred as 0.5% cheaper than it is.
        enable_two_hop: emit synthetic `AED → <intermediate> → BTC` routes
            within the same exchange. Default off for backward compat; turn
            on after verifying via `bitcoiners-dca routes`.
        intermediates: list of asset codes to use as intermediates for
            two-hop route generation (typically ["USDT"]).
        enable_cross_exchange_alerts: compute cross-exchange routes (e.g.
            buy USDT on OKX, withdraw to Binance, buy BTC) and surface them
            as alerts in the RoutingDecision. Never auto-executed.
        cross_exchange_min_size_aed: only emit a cross-exchange alert if
            the route is net-positive at this cycle size (after the fixed
            withdrawal cost).
        cross_exchange_withdrawal_costs: per-asset withdrawal fee, in the
            asset's units. Used to model the bridge math. Example:
            {"USDT": 1.5} for OKX TRC20.
    """

    def __init__(
        self,
        exclude_if_spread_pct_above: Decimal = Decimal("2.0"),
        preferred_exchange: Optional[str] = None,
        preferred_bonus_pct: Decimal = Decimal("0.5"),
        enable_two_hop: bool = False,
        intermediates: Optional[list[str]] = None,
        enable_cross_exchange_alerts: bool = False,
        cross_exchange_min_size_aed: Decimal = Decimal("25000"),
        cross_exchange_withdrawal_costs: Optional[dict[str, Decimal]] = None,
        prefer_intermediate_balance: bool = False,
        prefer_intermediate_min: Decimal = Decimal("10"),
        prefer_intermediate_boost_pct: Decimal = Decimal("1.0"),
    ):
        self.exclude_if_spread_pct_above = exclude_if_spread_pct_above
        self.preferred_exchange = preferred_exchange
        self.preferred_bonus_pct = preferred_bonus_pct
        self.enable_two_hop = enable_two_hop
        self.intermediates = intermediates or ["USDT", "USDC"]
        self.enable_cross_exchange_alerts = enable_cross_exchange_alerts
        self.cross_exchange_min_size_aed = cross_exchange_min_size_aed
        self.cross_exchange_withdrawal_costs = cross_exchange_withdrawal_costs or {}
        # Prefer existing stable-coin balance as the funding leg —
        # turns on a small downward score nudge for intermediate-direct
        # candidates (BTC/USDT or BTC/USDC paid from idle stable) so
        # they win over BTC/AED direct even when raw effective-price
        # ranking puts AED-direct marginally ahead. See score
        # application in _enumerate_same_exchange.
        self.prefer_intermediate_balance = prefer_intermediate_balance
        self.prefer_intermediate_min = prefer_intermediate_min
        self.prefer_intermediate_boost_pct = prefer_intermediate_boost_pct

    async def pick(
        self,
        exchanges: list[Exchange],
        pair: str = "BTC/AED",
        required_quote_amount: Optional[Decimal] = None,
        license_token: Optional[str] = None,
    ) -> RoutingDecision:
        target_asset, quote_ccy = pair.split("/")

        # Always gather market data first — both the remote and local paths
        # need it. Keeping the gather here (not inside _remote_pick) means
        # the local fallback is free if the remote call fails.
        market_data = await self._gather_market_data(
            exchanges, target_asset, quote_ccy
        )

        # If the hosted Pro API is configured AND the caller passed a
        # license token, try the remote pick first. Any failure (network,
        # 4xx/5xx, or `stub:true` response) returns None and we fall
        # through to the local implementation below — no behavior change
        # for Free-tier / self-hosters.
        if _PRO_API_URL and license_token:
            try:
                remote = await _remote_pick(
                    license_token, pair, required_quote_amount, market_data,
                    self.preferred_exchange,
                    self.preferred_bonus_pct,
                    self.exclude_if_spread_pct_above,
                    self.enable_two_hop,
                    self.intermediates,
                )
                if remote is not None:
                    return remote
            except Exception as e:  # noqa: BLE001 — defensive: never let
                # remote failure break a cycle
                logger.warning(
                    "[pro-api] remote pick raised, falling back to local: %s", e
                )

        executable, cross_alerts = self._enumerate_routes(
            market_data, target_asset, quote_ccy, required_quote_amount
        )

        usable, excluded = self._apply_filters(executable, required_quote_amount)
        if not usable:
            if excluded:
                # All routes filtered out by partner min. Give the user a
                # surgical error so the dashboard can render "BitOasis
                # needs AED 14, your cycle is AED 10" instead of a
                # generic no-route message.
                reasons = "; ".join(e.reason for e in excluded)
                raise RuntimeError(
                    f"No route can execute at this cycle size: {reasons}"
                )
            raise RuntimeError(
                f"No usable route to {target_asset} from {quote_ccy} "
                f"across enabled exchanges"
            )

        chosen = usable[0]
        chosen.note = "Selected: lowest effective price"

        alternatives = usable[1:]
        reason = (
            f"Picked {chosen.label} @ effective {chosen.effective_price:.2f} "
            f"{quote_ccy}/{target_asset}"
        )
        if alternatives:
            best_alt = alternatives[0]
            reason += (
                f" (next: {best_alt.label} @ {best_alt.effective_price:.2f})"
            )
        if required_quote_amount is not None and chosen.quote_balance is not None:
            reason += (
                f" · balance OK ({chosen.quote_balance} "
                f"{chosen.route.input_ccy} available)"
            )

        return RoutingDecision(
            chosen=chosen,
            alternatives=alternatives,
            cross_exchange_alerts=cross_alerts,
            excluded=excluded,
            reason=reason,
        )

    # === Internals ===

    async def _gather_market_data(
        self,
        exchanges: list[Exchange],
        target_asset: str,
        quote_ccy: str,
    ) -> list[_ExchangeMarketData]:
        """Fetch all the tickers + fees + balances we might need, in parallel."""
        pairs_to_try = [f"{target_asset}/{quote_ccy}"]  # direct
        if self.enable_two_hop or self.enable_cross_exchange_alerts:
            for inter in self.intermediates:
                if inter == quote_ccy or inter == target_asset:
                    continue
                pairs_to_try.append(f"{inter}/{quote_ccy}")        # hop 1
                pairs_to_try.append(f"{target_asset}/{inter}")     # hop 2
        # 3-hop cross-stable pairs (USDT/USDC, USDC/USDT, …). Required
        # so the enumerator can chain AED→i1→i2→BTC even when the
        # venue doesn't list the direct target/i1 pair. Cheap to add —
        # exchanges 404 on missing tickers and the gather step ignores
        # those without bubbling up an error.
        if self.enable_two_hop:
            for i1 in self.intermediates:
                if i1 == quote_ccy or i1 == target_asset:
                    continue
                for i2 in self.intermediates:
                    if i2 == i1 or i2 == quote_ccy or i2 == target_asset:
                        continue
                    pairs_to_try.append(f"{i2}/{i1}")
        pairs_to_try = list(dict.fromkeys(pairs_to_try))  # de-dup, preserve order

        # Also fetch balances for every intermediate (USDT, etc.) — we
        # use those to enumerate "intermediate-direct" routes that skip
        # the AED leg when the user already holds the intermediate. This
        # is how `Use USDT first` works: a 224-USDT idle balance on OKX
        # lets the bot route BTC/USDT directly instead of AED→USDT→BTC.
        ccys_to_balance = [quote_ccy] + [
            i for i in self.intermediates if i != quote_ccy and i != target_asset
        ]

        async def for_one(ex: Exchange) -> _ExchangeMarketData:
            ticker_tasks = [ex.get_ticker(p) for p in pairs_to_try]
            # Per-pair fee fetch. ccxt's load_markets returns per-pair
            # maker/taker, so get_fee_schedule(pair) is cheap (cached).
            # OKX charges ~6× more on AED-quoted pairs vs USDT pairs;
            # without per-pair fees the router prices multi-hop routes
            # incorrectly (audit 2026-05-24).
            fee_tasks = [ex.get_fee_schedule(p) for p in pairs_to_try]
            balance_tasks = [ex.get_balance(c) for c in ccys_to_balance]
            min_tasks = [ex.get_order_minimum(p) for p in pairs_to_try]
            results = await asyncio.gather(
                *ticker_tasks, *fee_tasks, *balance_tasks, *min_tasks,
                return_exceptions=True,
            )
            n_pairs = len(pairs_to_try)
            n_bal = len(ccys_to_balance)
            tickers_raw = results[:n_pairs]
            fees_raw_list = results[n_pairs : 2 * n_pairs]
            balances_raw = results[2 * n_pairs : 2 * n_pairs + n_bal]
            mins_raw = results[2 * n_pairs + n_bal :]

            tickers: dict[str, Ticker] = {}
            for p, t in zip(pairs_to_try, tickers_raw):
                if not isinstance(t, Exception):
                    tickers[p] = t

            # Per-pair fee map + a fallback default.
            taker_pct_by_pair: dict[str, Decimal] = {}
            default_taker = Decimal("0.005")  # conservative default
            for p, f in zip(pairs_to_try, fees_raw_list):
                if not isinstance(f, Exception):
                    taker_pct_by_pair[p] = f.taker_pct
            # Default = the direct pair's taker (preserves prior behaviour
            # for callers that read `.taker_pct` directly).
            if pairs_to_try[0] in taker_pct_by_pair:
                default_taker = taker_pct_by_pair[pairs_to_try[0]]

            balances: dict[str, Decimal] = {}
            for c, b in zip(ccys_to_balance, balances_raw):
                if not isinstance(b, Exception):
                    balances[c] = b.free if b else Decimal(0)

            minimums: dict[str, OrderMinimum] = {}
            for p, m in zip(pairs_to_try, mins_raw):
                if not isinstance(m, Exception):
                    minimums[p] = m

            return _ExchangeMarketData(
                exchange=ex, tickers=tickers, taker_pct=default_taker,
                balances=balances, minimums=minimums,
                taker_pct_by_pair=taker_pct_by_pair,
            )

        return await asyncio.gather(*[for_one(e) for e in exchanges])

    def _lookup_minimums(
        self,
        route: TradeRoute,
        market_data: list[_ExchangeMarketData],
    ) -> tuple[Optional[OrderMinimum], ...]:
        """For each hop, find the OrderMinimum on the hop's exchange/pair.

        Returns None for the hop when the adapter returned no info — the
        downstream min calculation skips Nones (treats them as no floor).
        """
        by_name = {md.exchange.name: md for md in market_data}
        out: list[Optional[OrderMinimum]] = []
        for hop in route.hops:
            md = by_name.get(hop.exchange)
            om = md.minimums.get(hop.pair) if md else None
            out.append(om)
        return tuple(out)

    def _enumerate_routes(
        self,
        market_data: list[_ExchangeMarketData],
        target_asset: str,
        quote_ccy: str,
        required_amount: Optional[Decimal],
    ) -> tuple[list[RouteCandidate], list[RouteCandidate]]:
        """Build (executable, cross_exchange_alerts) candidate lists."""
        executable: list[RouteCandidate] = []
        cross: list[RouteCandidate] = []
        sample_amount = required_amount if required_amount else Decimal(1000)

        for md in market_data:
            # Direct route
            direct_pair = f"{target_asset}/{quote_ccy}"
            if direct_pair in md.tickers:
                hop = TradeHop(
                    exchange=md.exchange.name, pair=direct_pair, side="buy",
                    price=md.tickers[direct_pair].ask,
                    taker_pct=md.taker_for(direct_pair),
                )
                route = TradeRoute(
                    hops=(hop,),
                    quote_balance=md.balances.get(quote_ccy),
                )
                executable.append(self._score(
                    route, sample_amount,
                    md.tickers[direct_pair].spread_pct, market_data,
                ))

            # Same-exchange two-hop via each intermediate
            if self.enable_two_hop:
                for inter in self.intermediates:
                    if inter == quote_ccy or inter == target_asset:
                        continue
                    leg1, leg2 = f"{inter}/{quote_ccy}", f"{target_asset}/{inter}"
                    if leg1 in md.tickers and leg2 in md.tickers:
                        hops = (
                            TradeHop(md.exchange.name, leg1, "buy",
                                     md.tickers[leg1].ask, md.taker_for(leg1)),
                            TradeHop(md.exchange.name, leg2, "buy",
                                     md.tickers[leg2].ask, md.taker_for(leg2)),
                        )
                        route = TradeRoute(
                            hops=hops,
                            quote_balance=md.balances.get(quote_ccy),
                        )
                        max_spread = max(
                            md.tickers[leg1].spread_pct,
                            md.tickers[leg2].spread_pct,
                        )
                        executable.append(self._score(
                            route, sample_amount, max_spread, market_data,
                        ))

                    # Intermediate-direct: if we already hold this
                    # intermediate (e.g. USDT sitting idle on OKX), we can
                    # skip leg-1 entirely and just BTC/USDT. The held balance
                    # is in the intermediate's units (USDT); we convert it to
                    # an AED-equivalent so it can be compared against the AED
                    # cycle size in one consistent unit, and we carry the
                    # AED→USDT rate so the strategy sizes the order in USDT.
                    # Threshold = 10 units of intermediate — below this it's
                    # noise (OKX BTC/USDT minimum is ~5 USDT). A 2-USDT dust
                    # balance wouldn't ever fund a trade so emitting the route
                    # pollutes the audit UI. (Audit 2026-06-02 task #212.)
                    inter_balance = md.balances.get(inter, Decimal(0))
                    direct_pair_via_inter = f"{target_asset}/{inter}"
                    if (
                        inter_balance >= self.prefer_intermediate_min
                        and direct_pair_via_inter in md.tickers
                    ):
                        # AED-per-intermediate from the <inter>/AED ticker
                        # (the same pair as the two-hop leg-1). Without it we
                        # cannot convert units, so the route is unusable.
                        inter_quote_tk = md.tickers.get(f"{inter}/{quote_ccy}")
                        aed_per_inter = (
                            inter_quote_tk.ask
                            if inter_quote_tk and inter_quote_tk.ask and inter_quote_tk.ask > 0
                            else None
                        )
                        if aed_per_inter is None:
                            # No <inter>/<quote> ticker → no conversion rate.
                            # The route could neither be ranked in quote units
                            # (its native price is ~FX-rate smaller, so it
                            # would always "win") nor sized in input units
                            # (the quote budget would be spent raw as <inter>,
                            # an ~FX-rate overspend). Skip it — the two-hop
                            # route via the same intermediate remains
                            # available. (Audit 2026-06-10 P0/P1.)
                            continue
                        quote_bal_aed = inter_balance * aed_per_inter
                        quote_to_input_rate = Decimal(1) / aed_per_inter
                        hop = TradeHop(
                            md.exchange.name,
                            direct_pair_via_inter,
                            "buy",
                            md.tickers[direct_pair_via_inter].ask,
                            md.taker_for(direct_pair_via_inter),
                        )
                        route = TradeRoute(
                            hops=(hop,),
                            quote_balance=quote_bal_aed,
                            quote_to_input_rate=quote_to_input_rate,
                        )
                        candidate = self._score(
                            route, sample_amount,
                            md.tickers[direct_pair_via_inter].spread_pct,
                            market_data,
                        )
                        # "Prefer existing stable balance" — small downward
                        # nudge so it wins over a marginally cheaper BTC/AED
                        # direct. Carried as a score_multiplier applied once
                        # in _apply_filters; setting candidate.score here
                        # directly was overwritten by the final scoring pass,
                        # making the preference dead code (audit 2026-06-02).
                        if self.prefer_intermediate_balance:
                            candidate.score_multiplier = Decimal(1) - (
                                self.prefer_intermediate_boost_pct / Decimal(100)
                            )
                            candidate.note = (
                                (candidate.note + " · " if candidate.note else "")
                                + f"prefer-{inter}-balance"
                            )
                        executable.append(candidate)

            # Same-exchange three-hop via two distinct intermediates,
            # e.g. AED→USDC→USDT→BTC. Captures the rare case where a
            # venue lists every leg of a chain but not the direct
            # target/quote or target/<single-intermediate> pair, OR
            # where the chained price beats the 2-hop alternatives
            # despite the extra fee drag. On BitOasis specifically,
            # AED→USDC→USDT→BTC is the only way to spend USDC since
            # BitOasis has no BTC/USDC. Cubic enumeration but small N
            # (3 intermediates → 6 ordered pairs) and gated on
            # enable_two_hop so single-hop users pay zero cost.
            if self.enable_two_hop:
                for i1 in self.intermediates:
                    if i1 == quote_ccy or i1 == target_asset:
                        continue
                    leg1 = f"{i1}/{quote_ccy}"
                    if leg1 not in md.tickers:
                        continue
                    for i2 in self.intermediates:
                        if i2 == i1 or i2 == quote_ccy or i2 == target_asset:
                            continue
                        leg2 = f"{i2}/{i1}"
                        leg3 = f"{target_asset}/{i2}"
                        if leg2 not in md.tickers or leg3 not in md.tickers:
                            continue
                        hops = (
                            TradeHop(md.exchange.name, leg1, "buy",
                                     md.tickers[leg1].ask, md.taker_for(leg1)),
                            TradeHop(md.exchange.name, leg2, "buy",
                                     md.tickers[leg2].ask, md.taker_for(leg2)),
                            TradeHop(md.exchange.name, leg3, "buy",
                                     md.tickers[leg3].ask, md.taker_for(leg3)),
                        )
                        route = TradeRoute(
                            hops=hops,
                            quote_balance=md.balances.get(quote_ccy),
                        )
                        max_spread = max(
                            md.tickers[leg1].spread_pct,
                            md.tickers[leg2].spread_pct,
                            md.tickers[leg3].spread_pct,
                        )
                        executable.append(self._score(
                            route, sample_amount, max_spread, market_data,
                        ))

        # Cross-exchange routes (alerts only)
        if self.enable_cross_exchange_alerts:
            cross = self._enumerate_cross_exchange(
                market_data, target_asset, quote_ccy, sample_amount,
            )

        return executable, cross

    def _enumerate_cross_exchange(
        self,
        market_data: list[_ExchangeMarketData],
        target_asset: str,
        quote_ccy: str,
        sample_amount: Decimal,
    ) -> list[RouteCandidate]:
        out: list[RouteCandidate] = []
        # Only emit if alert size is met
        if sample_amount < self.cross_exchange_min_size_aed:
            return out

        for src in market_data:
            for inter in self.intermediates:
                leg1 = f"{inter}/{quote_ccy}"
                if leg1 not in src.tickers:
                    continue
                for dst in market_data:
                    if dst.exchange.name == src.exchange.name:
                        continue
                    leg2 = f"{target_asset}/{inter}"
                    if leg2 not in dst.tickers:
                        continue
                    withdraw_fee_inter = self.cross_exchange_withdrawal_costs.get(
                        inter, Decimal(0)
                    )
                    # Express withdrawal fee in the input ccy (quote) at hop-1 rate
                    fixed_cost_in_quote = (
                        withdraw_fee_inter * src.tickers[leg1].ask
                    )
                    hops = (
                        TradeHop(src.exchange.name, leg1, "buy",
                                 src.tickers[leg1].ask, src.taker_for(leg1)),
                        TradeHop(dst.exchange.name, leg2, "buy",
                                 dst.tickers[leg2].ask, dst.taker_for(leg2)),
                    )
                    route = TradeRoute(
                        hops=hops,
                        cross_exchange=True,
                        fixed_costs=fixed_cost_in_quote,
                    )
                    max_spread = max(
                        src.tickers[leg1].spread_pct, dst.tickers[leg2].spread_pct
                    )
                    out.append(self._score(
                        route, sample_amount, max_spread, market_data,
                    ))
        # Sort by effective price; the most attractive alert first.
        out.sort(key=lambda c: c.effective_price)
        return out

    def _score(
        self,
        route: TradeRoute,
        sample_amount: Decimal,
        max_spread_pct: Decimal,
        market_data: list[_ExchangeMarketData],
    ) -> RouteCandidate:
        eff = _effective_price_in_quote(route, sample_amount)
        hop_mins = self._lookup_minimums(route, market_data)
        try:
            min_input = route.min_input_amount(hop_mins)
        except (NotImplementedError, ValueError):
            # Non-buy hops would raise; current router only emits buys.
            # Treat as no-floor so we don't accidentally exclude a route
            # whose math we can't evaluate.
            min_input = Decimal(0)
        return RouteCandidate(
            route=route,
            effective_price=eff,
            score=eff,
            max_spread_pct=max_spread_pct,
            quote_balance=route.quote_balance,
            min_input_amount=min_input,
            hop_minimums=hop_mins,
        )

    def _apply_filters(
        self,
        candidates: list[RouteCandidate],
        required_amount: Optional[Decimal],
    ) -> tuple[list[RouteCandidate], list[ExcludedRoute]]:
        excluded: list[ExcludedRoute] = []

        # Partner-minimum filter (only applies when we have a cycle size).
        # A route is excluded if cycle_amount < route.min_input_amount.
        # We only filter — never fall back — because trying to buy below a
        # partner's published floor returns a hard API rejection that
        # cancels the cycle. Better to skip the venue than burn a cycle.
        if required_amount is not None:
            kept: list[RouteCandidate] = []
            for c in candidates:
                # min_input_amount is in the route's INPUT currency. For an
                # intermediate-direct route that's USDT, but required_amount
                # is AED — convert the floor to AED before comparing so we
                # don't test a USDT floor against an AED cycle size (#212).
                floor_in_quote = c.min_input_amount
                rate = c.route.quote_to_input_rate
                if rate and rate > 0 and c.min_input_amount > 0:
                    floor_in_quote = c.min_input_amount / rate
                if floor_in_quote > 0 and required_amount < floor_in_quote:
                    excluded.append(ExcludedRoute(
                        route=c.route,
                        reason=(
                            f"Cycle {required_amount} below partner minimum "
                            f"{c.min_input_amount:.2f} {c.route.input_ccy} "
                            f"({_format_min_reason(c.hop_minimums)})"
                        ),
                        min_input_amount=c.min_input_amount,
                        hop_minimums=c.hop_minimums,
                    ))
                else:
                    kept.append(c)
            candidates = kept

        # Spread filter
        usable = [
            c for c in candidates
            if c.max_spread_pct <= self.exclude_if_spread_pct_above
        ]
        if not usable:
            usable = candidates[:]  # all wide; fall back to all candidates

        # Balance filter (None = trust user; treat 0 as "underfunded")
        underfunded_fallback = False
        if required_amount is not None:
            funded = [
                c for c in usable
                if c.quote_balance is None or c.quote_balance >= required_amount
            ]
            if funded:
                usable = funded
            else:
                # No exchange holds enough quote currency to fund the full
                # ask. Don't drop all routes — the bot still wants to make
                # progress on whatever balance IS available. Mark this
                # case so scoring (below) preferes the route with the
                # MOST usable balance, not just the cheapest. Otherwise a
                # near-empty exchange wins on price-per-coin while a
                # well-funded one sits idle.
                underfunded_fallback = True

        # Preference bonus — applies to candidates whose FIRST hop is on the
        # preferred exchange. For two-hop, that's the AED-spending leg.
        for c in usable:
            first_ex = c.route.hops[0].exchange
            if self.preferred_exchange and first_ex == self.preferred_exchange:
                c.score = c.effective_price * (
                    Decimal(1) - self.preferred_bonus_pct / Decimal(100)
                )
                c.note = (
                    (c.note + " · " if c.note else "")
                    + "Preferred-exchange bonus applied"
                )
            else:
                c.score = c.effective_price
            # Apply any per-candidate preference nudge ONCE, here, after the
            # base score is (re)assigned — e.g. the prefer-stablecoin boost
            # set at enumeration. Lower score = better, so a multiplier < 1
            # promotes the candidate.
            c.score = c.score * c.score_multiplier

        if underfunded_fallback:
            # Sort by quote_balance DESC (most usable balance first), then
            # by score (cheapest among the well-funded). Routes with no
            # reported balance sort last via the `or 0` fallback. This
            # makes the bot route to the exchange that can actually pay,
            # not the one with the prettiest theoretical price.
            usable.sort(key=lambda c: (-(c.quote_balance or Decimal(0)), c.score))
        else:
            usable.sort(key=lambda c: c.score)
        return usable, excluded


def _format_min_reason(
    hop_minimums: tuple[Optional[OrderMinimum], ...],
) -> str:
    """One-line summary of the binding minimum for an excluded route.

    Used to populate ExcludedRoute.reason so the UI can render
    "BitOasis min 0.000048 BTC" verbatim without re-fetching.
    """
    parts: list[str] = []
    for om in hop_minimums:
        if om is None:
            continue
        bits: list[str] = []
        if om.min_base is not None and om.min_base > 0:
            bits.append(f"{om.min_base.normalize():f} base")
        if om.min_quote is not None and om.min_quote > 0:
            bits.append(f"{om.min_quote.normalize():f} {om.quote_currency}")
        if bits:
            parts.append(f"{om.exchange}:{om.pair} ≥ " + " / ".join(bits))
    return "; ".join(parts) if parts else "partner minimum"


def _market_data_to_payload(
    market_data: list["_ExchangeMarketData"],
    target_asset: str,
    quote_ccy: str,
    intermediates: list[str],
) -> list[dict]:
    """Serialize per-exchange market data for the Pro API.

    Includes the direct pair (BTC/AED) plus, for each intermediate,
    the two pairs needed to synthesize a two-hop route. Balances are
    sent for the quote currency AND each intermediate, which lets the
    server emit the "intermediate-direct" (already-held USDT) shortcut.
    """
    direct_pair = f"{target_asset}/{quote_ccy}"
    wanted_pairs = {direct_pair}
    for inter in intermediates:
        if inter == quote_ccy or inter == target_asset:
            continue
        wanted_pairs.add(f"{inter}/{quote_ccy}")
        wanted_pairs.add(f"{target_asset}/{inter}")

    payload: list[dict] = []
    for md in market_data:
        # Skip exchanges that don't carry the direct pair AND can't
        # serve any two-hop path either.
        tickers_out: dict[str, dict] = {}
        for p in wanted_pairs:
            t = md.tickers.get(p)
            if t is None or t.ask <= 0:
                continue
            tickers_out[p] = {
                "ask": float(t.ask),
                "bid": float(t.bid),
                "spread_pct": float(t.spread_pct),
            }
        if not tickers_out:
            continue

        balances_out = {
            quote_ccy: float(md.balances.get(quote_ccy, Decimal(0))),
        }
        for inter in intermediates:
            if inter in (quote_ccy, target_asset):
                continue
            balances_out[inter] = float(md.balances.get(inter, Decimal(0)))

        # Per-pair taker fees so a fee-aware server can price each hop with
        # its own rate. The scalar `taker_pct` is the DIRECT pair's fee
        # (~0.6% on OKX AED); applying it to a BTC/USDT leg (~0.1%) overprices
        # two-hop routes and biases the server back to direct (audit
        # 2026-06-02). Sent alongside the scalar for backward compatibility;
        # the bot also re-prices locally (see _reprice_decision_with_local_fees)
        # so correctness holds even against a server that ignores this.
        taker_by_pair_out = {
            p: float(v) for p, v in md.taker_pct_by_pair.items()
            if p in tickers_out
        }
        payload.append({
            "exchange": md.exchange.name,
            "tickers": tickers_out,
            "taker_pct": float(md.taker_pct),
            "taker_pct_by_pair": taker_by_pair_out,
            "balances": balances_out,
        })
    return payload


def _decode_remote_decision(
    data: dict,
    pair: str,
    market_data: list["_ExchangeMarketData"],
) -> Optional[RoutingDecision]:
    """Translate the /api/pro/route JSON response into a RoutingDecision.

    The server's response shape (non-stub) is:
        { chosen: {label, exchange, hops:[{exchange,pair,side,price,taker_pct}],
                   effective_price, max_spread_pct, quote_balance, note?},
          alternatives: [...same shape...],
          reason: string,
          stub: false }

    Returns None if the shape is malformed — caller falls back to local.

    Currency safety (audit 2026-06-10 P0): the wire format has no
    quote_to_input_rate field, but the server emits "held-USDT"
    intermediate-direct candidates whose input currency is NOT the cycle
    quote. Rebuilding those without a rate re-creates the #212 bug through
    the remote path: the AED budget number gets spent as raw USDT (~3.67x
    overspend), the balance clamp compares mixed units, and the local
    re-pricing ranks the USDT-denominated price as ~3.67x "cheaper" so the
    broken route always wins. We therefore reconstruct the rate from the
    bot's OWN tickers (never trust the wire for a unit-conversion factor)
    and reject any candidate whose rate can't be derived locally.
    """
    quote_ccy = pair.split("/")[1]

    def _to_candidate(c: dict) -> RouteCandidate:
        hops_raw = c.get("hops") or []
        if not hops_raw:
            raise ValueError("candidate has no hops")
        hops = tuple(
            TradeHop(
                exchange=h["exchange"],
                pair=h["pair"],
                side=h["side"],
                price=Decimal(str(h["price"])),
                taker_pct=Decimal(str(h["taker_pct"])),
            )
            for h in hops_raw
        )
        qb = c.get("quote_balance")
        qb = Decimal(str(qb)) if qb is not None else None
        rate = None
        input_ccy = hops[0].input_ccy
        if input_ccy != quote_ccy:
            local_ask = None
            for md in market_data:
                if md.exchange.name == hops[0].exchange:
                    tk = md.tickers.get(f"{input_ccy}/{quote_ccy}")
                    if tk is not None and tk.ask and tk.ask > 0:
                        local_ask = tk.ask
                    break
            if local_ask is None:
                raise ValueError(
                    f"route spends {input_ccy} but cycle quote is {quote_ccy} "
                    f"and no local {input_ccy}/{quote_ccy} ticker exists on "
                    f"{hops[0].exchange} to derive a conversion rate"
                )
            rate = Decimal(1) / local_ask
            # The server reports a held-intermediate balance in INPUT units
            # (e.g. raw USDT). Convert to quote units so the strategy's
            # balance clamp compares like-for-like — mirrors what the local
            # enumerator stores.
            if qb is not None:
                qb = qb / rate
        route = TradeRoute(
            hops=hops,
            quote_balance=qb,
            cross_exchange=False,
            quote_to_input_rate=rate,
        )
        return RouteCandidate(
            route=route,
            effective_price=Decimal(str(c["effective_price"])),
            score=Decimal(str(c["effective_price"])),
            max_spread_pct=Decimal(str(c.get("max_spread_pct", 0))),
            quote_balance=qb,
            note=c.get("note", ""),
        )

    try:
        chosen = _to_candidate(data["chosen"])
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(
            "[pro-api] cannot decode remote chosen route, falling back: %s", e
        )
        return None
    alternatives = []
    for c in data.get("alternatives", []):
        try:
            alternatives.append(_to_candidate(c))
        except (KeyError, TypeError, ValueError) as e:
            # A bad alternative shouldn't sink the whole remote decision —
            # drop it; the chosen route already decoded safely.
            logger.warning("[pro-api] dropping undecodable alternative: %s", e)

    return RoutingDecision(
        chosen=chosen,
        alternatives=alternatives,
        cross_exchange_alerts=[],   # Phase 2 follow-up
        reason=data.get("reason", "Remote pick"),
    )


def _reprice_decision_with_local_fees(
    decision: RoutingDecision,
    market_data: list["_ExchangeMarketData"],
    required_quote_amount: Optional[Decimal],
    pair: str,
) -> Optional[RoutingDecision]:
    """Re-rank a remote decision using the bot's OWN per-pair taker fees.

    The Pro API server prices hops from a single scalar taker (the direct
    pair's ~0.6% AED fee), which overprices the cheaper BTC/USDT leg of
    two-hop routes and biases selection back to direct (audit 2026-06-02).
    We trust the server only for route STRUCTURE (which hops exist) and
    recompute effective_price locally with the correct per-pair fee, then
    re-sort. This keeps fee correctness even if the server never consumes
    `taker_pct_by_pair`.

    Returns None (caller falls back to local routing) if no candidate can
    be ranked safely — a route spending a non-quote input currency without
    a conversion rate must never enter the sort: its numerically smaller
    price would always "win" and then overspend by the FX rate at
    execution (audit 2026-06-10 P0).
    """
    quote_ccy = pair.split("/")[1]
    taker_by: dict[tuple[str, str], Decimal] = {}
    for md in market_data:
        for p in md.tickers:
            taker_by[(md.exchange.name, p)] = md.taker_for(p)

    sample = required_quote_amount if required_quote_amount else Decimal(1000)

    def reprice(c: RouteCandidate) -> RouteCandidate:
        new_hops = tuple(
            TradeHop(
                exchange=h.exchange, pair=h.pair, side=h.side, price=h.price,
                taker_pct=taker_by.get((h.exchange, h.pair), h.taker_pct),
            )
            for h in c.route.hops
        )
        new_route = TradeRoute(
            hops=new_hops,
            quote_balance=c.route.quote_balance,
            cross_exchange=c.route.cross_exchange,
            fixed_costs=c.route.fixed_costs,
            quote_to_input_rate=c.route.quote_to_input_rate,
        )
        c.route = new_route
        c.effective_price = _effective_price_in_quote(new_route, sample)
        c.score = c.effective_price
        return c

    all_c = [reprice(decision.chosen)] + [reprice(a) for a in decision.alternatives]
    safe = [
        c for c in all_c
        if c.route.input_ccy == quote_ccy
        or (c.route.quote_to_input_rate and c.route.quote_to_input_rate > 0)
    ]
    if len(safe) < len(all_c):
        logger.warning(
            "[pro-api] dropped %d remote candidate(s) spending a non-%s input "
            "with no conversion rate — unrankable cross-currency price",
            len(all_c) - len(safe), quote_ccy,
        )
    if not safe:
        return None
    safe.sort(key=lambda c: c.score)
    decision.chosen = safe[0]
    decision.alternatives = safe[1:]
    decision.reason = (
        f"Picked {decision.chosen.label} @ effective "
        f"{decision.chosen.effective_price:.2f} (Pro API route, locally "
        f"re-priced with per-pair fees)"
    )
    return decision


async def _remote_pick(
    license_token: str,
    pair: str,
    required_quote_amount: Optional[Decimal],
    market_data: list["_ExchangeMarketData"],
    preferred_exchange: Optional[str],
    preferred_bonus_pct: Decimal,
    exclude_if_spread_pct_above: Decimal,
    enable_two_hop: bool,
    intermediates: list[str],
) -> Optional[RoutingDecision]:
    """Call /api/pro/route on the hosted Pro API with full market data.

    Returns a `RoutingDecision` if the remote returns an authoritative
    result. Returns None if the remote is unreachable, returns an error,
    or returns `stub:true`. The caller falls back to local logic on None.
    """
    if not _PRO_API_URL:
        return None
    try:
        import httpx  # local import — httpx is already a dep for ccxt async
    except ImportError:
        logger.warning("[pro-api] httpx not available, skipping remote pick")
        return None

    target_asset, quote_ccy = pair.split("/")
    market_payload = _market_data_to_payload(
        market_data, target_asset, quote_ccy, intermediates,
    )
    if not market_payload:
        return None

    body = {
        "pair": pair,
        "required_quote_amount": (
            str(required_quote_amount) if required_quote_amount is not None else None
        ),
        "market_data": market_payload,
        "preferred_exchange": preferred_exchange,
        "preferred_bonus_pct": float(preferred_bonus_pct),
        "exclude_if_spread_pct_above": float(exclude_if_spread_pct_above),
        "enable_two_hop": enable_two_hop,
        "intermediates": intermediates,
    }
    from bitcoiners_dca.core import pro_api_status  # local — avoid import cycles

    try:
        async with httpx.AsyncClient(timeout=_PRO_API_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{_PRO_API_URL}/api/pro/route",
                headers={"Authorization": f"Bearer {license_token}"},
                json=body,
            )
    except httpx.HTTPError as e:
        logger.warning("[pro-api] /api/pro/route call failed: %s", e)
        await pro_api_status.record_fallback("/api/pro/route", f"network error: {e}")
        return None

    if resp.status_code == 401:
        logger.warning(
            "[pro-api] license rejected by /api/pro/route — using local logic. "
            "Check that your license key in config.yaml matches the active "
            "subscription on bitcoiners.ae."
        )
        await pro_api_status.record_fallback(
            "/api/pro/route", "license token rejected (HTTP 401)",
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "[pro-api] /api/pro/route HTTP %s, falling back to local logic",
            resp.status_code,
        )
        await pro_api_status.record_fallback(
            "/api/pro/route", f"HTTP {resp.status_code}",
        )
        return None

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[pro-api] /api/pro/route returned non-JSON: %s", e)
        await pro_api_status.record_fallback("/api/pro/route", "malformed response")
        return None

    if data.get("stub"):
        logger.info(
            "[pro-api] /api/pro/route returned stub:true — falling back to "
            "local SmartRouter for this cycle (%s)",
            data.get("rationale", "no rationale"),
        )
        await pro_api_status.record_fallback(
            "/api/pro/route",
            f"server returned stub: {data.get('rationale', 'no rationale')}",
        )
        return None

    decision = _decode_remote_decision(data, pair, market_data)
    if decision is None:
        await pro_api_status.record_fallback(
            "/api/pro/route", "malformed response (decode failed)",
        )
        return None

    # Re-price with local per-pair fees so a fee-blind server can't bias the
    # pick (audit 2026-06-02 pro-api-payload-drops-per-pair-fees).
    decision = _reprice_decision_with_local_fees(
        decision, market_data, required_quote_amount, pair
    )
    if decision is None:
        await pro_api_status.record_fallback(
            "/api/pro/route", "no safely-rankable remote candidate",
        )
        return None
    logger.info(
        "[pro-api] remote pick: %s @ %s",
        decision.chosen.label,
        decision.chosen.effective_price,
    )
    await pro_api_status.record_success("/api/pro/route")
    return decision
