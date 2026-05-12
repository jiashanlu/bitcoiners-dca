"""
Trade routing — multi-hop path model for getting from quote currency to BTC.

A `TradeRoute` is an ordered list of `TradeHop`s that, when executed sequentially,
takes the user from a quote currency (e.g. AED) to BTC. Single-hop direct routes
(buy BTC/AED in one shot) and two-hop synthetic routes (AED → USDT → BTC, both
on the same exchange) share the same abstraction.

Why this matters: at most cycle sizes, the synthetic two-hop AED→USDT→BTC route
on OKX yields ~0.3% more BTC than the direct AED→BTC route, because OKX's
BTC/AED pair carries a wider implicit spread than BTC/USDT × USDT/AED net of
two taker fees. See `docs/ROUTING.md` for the math and live numbers.

This module is pure: no I/O. The router uses it to *describe* candidate routes;
exchange adapters execute the individual hops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class TradeHop:
    """A single market-side action on one exchange.

    For an AED → BTC purchase:
      - Single-hop direct:  TradeHop("okx", "BTC/AED", "buy", ask=300_000, taker=0.0015)
      - Two-hop via USDT:   [
          TradeHop("okx", "USDT/AED", "buy", ask=3.67, taker=0.0015),
          TradeHop("okx", "BTC/USDT", "buy", ask=82_000, taker=0.0015),
        ]

    `price` is the ask (when buying) or bid (when selling) at the moment of
    route construction. The hop assumes a taker-side execution; the strategy
    layer is responsible for upgrading to a limit (maker) order when the user
    has chosen maker-mode execution.
    """
    exchange: str
    pair: str          # canonical "BASE/QUOTE", e.g. "BTC/USDT"
    side: str          # "buy" or "sell"
    price: Decimal     # ask (buy) or bid (sell)
    taker_pct: Decimal

    @property
    def base_ccy(self) -> str:
        return self.pair.split("/")[0]

    @property
    def quote_ccy(self) -> str:
        return self.pair.split("/")[1]

    @property
    def input_ccy(self) -> str:
        return self.quote_ccy if self.side == "buy" else self.base_ccy

    @property
    def output_ccy(self) -> str:
        return self.base_ccy if self.side == "buy" else self.quote_ccy

    def expected_output(self, input_amount: Decimal) -> Decimal:
        """How much of `output_ccy` we expect to receive after taker fee."""
        if self.side == "buy":
            return input_amount / (self.price * (Decimal(1) + self.taker_pct))
        return input_amount * self.price * (Decimal(1) - self.taker_pct)


@dataclass
class TradeRoute:
    """An ordered sequence of hops from the user's quote currency to BTC.

    Single-hop routes have `len(hops) == 1`. Two-hop routes (e.g. AED → USDT → BTC)
    chain hops where the output of hop K feeds hop K+1.

    `quote_balance` records the available balance of the route's *input* currency
    on the source exchange of hop 1, so the router can exclude routes that the
    user can't afford to execute.

    `cross_exchange` is true when the route crosses exchange boundaries (e.g.
    buy USDT on OKX, withdraw to Binance, buy BTC). These are NOT auto-executed
    by the bot today — only surfaced as Telegram alerts when net-positive.
    """
    hops: tuple[TradeHop, ...]
    quote_balance: Optional[Decimal] = None
    cross_exchange: bool = False
    fixed_costs: Decimal = Decimal(0)   # e.g. withdrawal fees in quote ccy

    def __post_init__(self):
        if not self.hops:
            raise ValueError("TradeRoute must have at least one hop")
        for prev, nxt in zip(self.hops[:-1], self.hops[1:]):
            if prev.output_ccy != nxt.input_ccy:
                raise ValueError(
                    f"Route hops don't chain: {prev.output_ccy} → "
                    f"expected {prev.output_ccy} in but next hop wants {nxt.input_ccy}"
                )

    @property
    def input_ccy(self) -> str:
        return self.hops[0].input_ccy

    @property
    def output_ccy(self) -> str:
        return self.hops[-1].output_ccy

    @property
    def is_direct(self) -> bool:
        return len(self.hops) == 1

    @property
    def exchanges_involved(self) -> tuple[str, ...]:
        seen: list[str] = []
        for h in self.hops:
            if h.exchange not in seen:
                seen.append(h.exchange)
        return tuple(seen)

    def expected_output(self, input_amount: Decimal) -> Decimal:
        """Simulate the whole route end-to-end at the recorded prices.

        Subtracts `fixed_costs` (in the input currency) before the first hop —
        used by cross-exchange routes to model withdrawal fees.
        """
        amount = input_amount - self.fixed_costs
        if amount <= 0:
            return Decimal(0)
        for hop in self.hops:
            amount = hop.expected_output(amount)
        return amount

    def effective_price(self, input_amount: Decimal = Decimal(1000)) -> Decimal:
        """Cost per unit of output currency (e.g. AED per BTC).

        Computed at a sample size because routes with fixed costs (cross-
        exchange withdrawal fees) have size-dependent effective prices.
        """
        out = self.expected_output(input_amount)
        if out == 0:
            return Decimal("Infinity")
        return input_amount / out

    @property
    def label(self) -> str:
        """Human-readable summary, e.g. 'okx: BTC/AED' or 'okx: AED→USDT→BTC'."""
        if self.is_direct:
            return f"{self.hops[0].exchange}: {self.hops[0].pair}"
        if len(self.exchanges_involved) == 1:
            ex = self.exchanges_involved[0]
            chain = self.input_ccy + "".join(
                f"→{h.output_ccy}" for h in self.hops
            )
            return f"{ex}: {chain}"
        # cross-exchange
        legs = " → ".join(f"{h.exchange}:{h.pair}" for h in self.hops)
        return f"cross: {legs}"
