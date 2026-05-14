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

from bitcoiners_dca.core.models import Ticker, FeeSchedule
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
    quote_balance: Optional[Decimal] = None
    note: str = ""

    @property
    def label(self) -> str:
        return self.route.label

    @property
    def is_cross_exchange(self) -> bool:
        return self.route.cross_exchange


@dataclass
class RoutingDecision:
    chosen: RouteCandidate
    alternatives: list[RouteCandidate]
    cross_exchange_alerts: list[RouteCandidate] = field(default_factory=list)
    reason: str = ""

    @property
    def best_alt(self) -> Optional[RouteCandidate]:
        return self.alternatives[0] if self.alternatives else None

    def price_premium_vs_alt_pct(self) -> Decimal:
        """How much MORE the next-best alternative would have cost."""
        if not self.best_alt:
            return Decimal(0)
        diff = self.best_alt.effective_price - self.chosen.effective_price
        return (diff / self.chosen.effective_price) * Decimal(100)


# === Quote bundle, fetched once per exchange to avoid duplicate ticker hits. ===

@dataclass
class _ExchangeMarketData:
    exchange: Exchange
    tickers: dict[str, Ticker]               # pair -> ticker (failed pairs absent)
    taker_pct: Decimal
    balances: dict[str, Decimal]             # asset -> free balance (0 if absent)


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
    ):
        self.exclude_if_spread_pct_above = exclude_if_spread_pct_above
        self.preferred_exchange = preferred_exchange
        self.preferred_bonus_pct = preferred_bonus_pct
        self.enable_two_hop = enable_two_hop
        self.intermediates = intermediates or ["USDT"]
        self.enable_cross_exchange_alerts = enable_cross_exchange_alerts
        self.cross_exchange_min_size_aed = cross_exchange_min_size_aed
        self.cross_exchange_withdrawal_costs = cross_exchange_withdrawal_costs or {}

    async def pick(
        self,
        exchanges: list[Exchange],
        pair: str = "BTC/AED",
        required_quote_amount: Optional[Decimal] = None,
        license_token: Optional[str] = None,
    ) -> RoutingDecision:
        # If the hosted Pro API is configured AND the caller passed a
        # license token, try the remote pick first. Any failure (network,
        # 4xx/5xx, or `stub:true` response) returns None and we fall
        # through to the local implementation below — no behavior change
        # for Free-tier / self-hosters.
        if _PRO_API_URL and license_token:
            try:
                remote = await _remote_pick(
                    license_token, required_quote_amount, exchanges
                )
                if remote is not None:
                    return remote
            except Exception as e:  # noqa: BLE001 — defensive: never let
                # remote failure break a cycle
                logger.warning(
                    "[pro-api] remote pick raised, falling back to local: %s", e
                )

        target_asset, quote_ccy = pair.split("/")

        market_data = await self._gather_market_data(
            exchanges, target_asset, quote_ccy
        )

        executable, cross_alerts = self._enumerate_routes(
            market_data, target_asset, quote_ccy, required_quote_amount
        )

        usable = self._apply_filters(executable, required_quote_amount)
        if not usable:
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
            fee_task = ex.get_fee_schedule(pairs_to_try[0])
            balance_tasks = [ex.get_balance(c) for c in ccys_to_balance]
            results = await asyncio.gather(
                *ticker_tasks, fee_task, *balance_tasks,
                return_exceptions=True,
            )
            tickers_raw = results[:len(pairs_to_try)]
            fees_raw = results[len(pairs_to_try)]
            balances_raw = results[len(pairs_to_try) + 1:]

            tickers: dict[str, Ticker] = {}
            for p, t in zip(pairs_to_try, tickers_raw):
                if not isinstance(t, Exception):
                    tickers[p] = t

            taker = Decimal("0.005")  # conservative default
            if not isinstance(fees_raw, Exception):
                taker = fees_raw.taker_pct

            balances: dict[str, Decimal] = {}
            for c, b in zip(ccys_to_balance, balances_raw):
                if not isinstance(b, Exception):
                    balances[c] = b.free if b else Decimal(0)
            return _ExchangeMarketData(
                exchange=ex, tickers=tickers, taker_pct=taker, balances=balances,
            )

        return await asyncio.gather(*[for_one(e) for e in exchanges])

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
                    price=md.tickers[direct_pair].ask, taker_pct=md.taker_pct,
                )
                route = TradeRoute(
                    hops=(hop,),
                    quote_balance=md.balances.get(quote_ccy),
                )
                executable.append(self._score(route, sample_amount,
                                              md.tickers[direct_pair].spread_pct))

            # Same-exchange two-hop via each intermediate
            if self.enable_two_hop:
                for inter in self.intermediates:
                    if inter == quote_ccy or inter == target_asset:
                        continue
                    leg1, leg2 = f"{inter}/{quote_ccy}", f"{target_asset}/{inter}"
                    if leg1 in md.tickers and leg2 in md.tickers:
                        hops = (
                            TradeHop(md.exchange.name, leg1, "buy",
                                     md.tickers[leg1].ask, md.taker_pct),
                            TradeHop(md.exchange.name, leg2, "buy",
                                     md.tickers[leg2].ask, md.taker_pct),
                        )
                        route = TradeRoute(
                            hops=hops,
                            quote_balance=md.balances.get(quote_ccy),
                        )
                        max_spread = max(
                            md.tickers[leg1].spread_pct,
                            md.tickers[leg2].spread_pct,
                        )
                        executable.append(self._score(route, sample_amount, max_spread))

                    # Intermediate-direct: if we already hold this
                    # intermediate (e.g. USDT sitting idle on OKX), we can
                    # skip leg-1 entirely and just BTC/USDT. The
                    # quote_balance is reported in the intermediate's
                    # units; balance-clamp in strategy.execute handles the
                    # conversion. Threshold = 10 units of intermediate —
                    # below this it's noise (OKX BTC/USDT minimum is ~5
                    # USDT). A 2-USDT dust balance wouldn't ever fund a
                    # trade so emitting the route pollutes the audit UI.
                    inter_balance = md.balances.get(inter, Decimal(0))
                    direct_pair_via_inter = f"{target_asset}/{inter}"
                    if (
                        inter_balance >= Decimal("10")
                        and direct_pair_via_inter in md.tickers
                    ):
                        hop = TradeHop(
                            md.exchange.name,
                            direct_pair_via_inter,
                            "buy",
                            md.tickers[direct_pair_via_inter].ask,
                            md.taker_pct,
                        )
                        route = TradeRoute(
                            hops=(hop,),
                            quote_balance=inter_balance,
                        )
                        executable.append(self._score(
                            route, sample_amount,
                            md.tickers[direct_pair_via_inter].spread_pct,
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
                                 src.tickers[leg1].ask, src.taker_pct),
                        TradeHop(dst.exchange.name, leg2, "buy",
                                 dst.tickers[leg2].ask, dst.taker_pct),
                    )
                    route = TradeRoute(
                        hops=hops,
                        cross_exchange=True,
                        fixed_costs=fixed_cost_in_quote,
                    )
                    max_spread = max(
                        src.tickers[leg1].spread_pct, dst.tickers[leg2].spread_pct
                    )
                    out.append(self._score(route, sample_amount, max_spread))
        # Sort by effective price; the most attractive alert first.
        out.sort(key=lambda c: c.effective_price)
        return out

    def _score(
        self,
        route: TradeRoute,
        sample_amount: Decimal,
        max_spread_pct: Decimal,
    ) -> RouteCandidate:
        eff = route.effective_price(sample_amount)
        return RouteCandidate(
            route=route,
            effective_price=eff,
            score=eff,
            max_spread_pct=max_spread_pct,
            quote_balance=route.quote_balance,
        )

    def _apply_filters(
        self,
        candidates: list[RouteCandidate],
        required_amount: Optional[Decimal],
    ) -> list[RouteCandidate]:
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
                c.note = "Preferred-exchange bonus applied"
            else:
                c.score = c.effective_price

        if underfunded_fallback:
            # Sort by quote_balance DESC (most usable balance first), then
            # by score (cheapest among the well-funded). Routes with no
            # reported balance sort last via the `or 0` fallback. This
            # makes the bot route to the exchange that can actually pay,
            # not the one with the prettiest theoretical price.
            usable.sort(key=lambda c: (-(c.quote_balance or Decimal(0)), c.score))
        else:
            usable.sort(key=lambda c: c.score)
        return usable


async def _remote_pick(
    license_token: str,
    aed_amount: Optional[Decimal],
    exchanges: list[Exchange],
) -> Optional[RoutingDecision]:
    """Call /api/pro/route on the hosted Pro API.

    Returns a `RoutingDecision` if the remote returns an authoritative
    result. Returns None if the remote is unreachable, returns an error,
    returns `stub:true`, or returns a shape we can't yet translate. The
    caller falls back to local logic on None.

    Decoupled from `SmartRouter.pick` so that adding new server-side
    Pro endpoints later (funding, arbitrage, etc.) can share the same
    HTTP plumbing without bloating the router class.
    """
    if not _PRO_API_URL:
        return None
    try:
        import httpx  # local import — httpx is already a dep for ccxt async
    except ImportError:
        logger.warning("[pro-api] httpx not available, skipping remote pick")
        return None

    available = [ex.name for ex in exchanges if getattr(ex, "name", None)]
    body = {
        "aed_amount": str(aed_amount) if aed_amount is not None else "0",
        "available_exchanges": available,
        "prefer_maker": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_PRO_API_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{_PRO_API_URL}/api/pro/route",
                headers={"Authorization": f"Bearer {license_token}"},
                json=body,
            )
    except httpx.HTTPError as e:
        logger.warning("[pro-api] /api/pro/route call failed: %s", e)
        return None

    if resp.status_code == 401:
        logger.warning(
            "[pro-api] license rejected by /api/pro/route — using local logic. "
            "Check that your license key in config.yaml matches the active "
            "subscription on bitcoiners.ae."
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "[pro-api] /api/pro/route HTTP %s, falling back to local logic",
            resp.status_code,
        )
        return None

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[pro-api] /api/pro/route returned non-JSON: %s", e)
        return None

    if data.get("stub"):
        logger.info(
            "[pro-api] /api/pro/route returned stub:true (Phase 2 vertical "
            "slice), falling back to local SmartRouter for this cycle"
        )
        return None

    # TODO: when the server stops returning stub:true, translate `data` into
    # a `RoutingDecision` here. Will involve building `RouteCandidate`s from
    # the JSON shape — see workspace/bitcoiners-pro-api-plan.md for the
    # response contract. For now we only know the auth + transport works,
    # which is the vertical slice goal.
    logger.warning(
        "[pro-api] /api/pro/route returned non-stub response but the bot "
        "doesn't yet know how to translate it — falling back to local. "
        "Update _remote_pick() to deserialize the new schema."
    )
    return None
