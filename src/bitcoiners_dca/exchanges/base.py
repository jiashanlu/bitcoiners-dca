"""
Exchange — the abstract base class every exchange adapter implements.

Adding a new exchange means writing one class that implements these methods.
Strategy, router, and reports never touch raw exchange APIs — they call this
interface and consume the normalized models in `core.models`.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional

from bitcoiners_dca.core.models import (
    Ticker, Balance, Order, OrderStatus, OrderMinimum, Withdrawal, FeeSchedule
)


# Prefix every order's clientOrderId with this so the pre-cycle sweep can
# distinguish bot orders from orders the customer placed manually on the
# exchange's own UI. Keep short (OKX clOrdId max 32 chars, alphanumeric).
BOT_CLORD_PREFIX = "bdca"


def _to_decimal_safe(value) -> Decimal:
    """Decimal coercion that tolerates None / empty string / float / int."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


def resolve_partial_status(mapped: OrderStatus, filled, amount) -> OrderStatus:
    """Upgrade a still-open order to PARTIAL when it carries a non-zero fill.

    Exchanges report a partially-filled-but-resting limit as status
    ``open``/``OPEN`` (``filled>0``, ``remaining>0``), which every adapter
    maps to PENDING. Before this, nothing ever produced ``OrderStatus.PARTIAL``,
    so every partial-fill branch in the strategy was dead code — and a maker
    order that partially filled before timeout was treated as wholly unfilled
    and re-bought in full (audit 2026-06-02 P0/P1). This derives PARTIAL from
    the fill quantities, adapter-agnostically.

    Only upgrades from PENDING. A terminal status (FILLED/CANCELLED/REJECTED)
    is authoritative and passes through unchanged.
    """
    if mapped != OrderStatus.PENDING:
        return mapped
    f = _to_decimal_safe(filled)
    if f <= 0:
        return mapped
    a = _to_decimal_safe(amount)
    # filled>0 and (amount unknown, or filled strictly below the order size)
    # → a resting partial. filled>=amount with an open status is ambiguous
    # (likely settling) — leave it PENDING and let the FILLED/closed mapping
    # promote it on the next poll.
    if a <= 0 or f < a:
        return OrderStatus.PARTIAL
    return mapped


def split_fee_by_currency(
    fee: object,
    pair: str,
) -> tuple[Decimal, Decimal]:
    """Route a CCXT-shaped fee `{cost, currency}` into (fee_base, fee_quote).

    CCXT normalizes order/trade fees as `{"cost": "0.0000003", "currency": "BTC"}`.
    For a BTC/AED buy, exchanges typically charge the fee in the *base* asset
    (BTC) — the previously-naive code put that value into `fee_quote` (AED) and
    the notification ended up displaying `AED 3.48E-7` which looks like a 20%
    fee at first glance (it's actually 0.16% in BTC terms).

    Returns (fee_base, fee_quote) — exactly one is populated based on the
    fee currency vs the pair's base/quote. Anything unrecognised falls
    back to `fee_quote` so the value isn't lost; better wrong column
    than wrong magnitude.
    """
    if not isinstance(fee, dict):
        return Decimal(0), Decimal(0)
    cost = _to_decimal_safe(fee.get("cost"))
    if cost == 0:
        return Decimal(0), Decimal(0)
    fee_ccy = (fee.get("currency") or "").upper().strip()
    if "/" in pair:
        base_ccy, _, quote_ccy = pair.upper().partition("/")
    else:
        base_ccy, quote_ccy = "BTC", "AED"
    if fee_ccy == base_ccy:
        return cost, Decimal(0)
    if fee_ccy == quote_ccy:
        return Decimal(0), cost
    # Unknown currency (rare — could be a bonus token or rebate
    # currency). Keep the value, mark as quote-side so it's at least
    # visible to the user — the rendered notification annotates with
    # the actual currency name.
    return Decimal(0), cost


def make_bot_client_order_id() -> str:
    """Generate a clientOrderId for a bot-placed order.

    Shape: `bdca` + 28 hex chars from a uuid4. Total 32, fits OKX +
    Binance limits. Identifiable in `cancel_all_open_orders` so we
    don't touch orders the user placed manually.
    """
    from uuid import uuid4
    return BOT_CLORD_PREFIX + uuid4().hex[:28]


class ExchangeError(Exception):
    """Base for exchange-specific failures (wraps underlying API errors)."""


class InsufficientBalanceError(ExchangeError):
    pass


class WithdrawalDeniedError(ExchangeError):
    """Withdrawal address not whitelisted, KYC limit reached, etc."""


class Exchange(ABC):
    """Adapter interface — every supported exchange implements this."""

    name: str        # e.g. "okx", "binance", "bitoasis"
    quote_currency: str = "AED"  # default; some exchanges may be USDT-only
    # Whether `withdraw_btc(..., network="lightning")` is implemented and
    # actually expected to succeed against this venue. Default False; OKX
    # overrides to True. The dashboard reads this to gate the LN field on
    # the per-exchange withdrawal form (no more lying about LN support).
    supports_lightning_withdrawal: bool = False

    # === IDENTITY / HEALTH ===

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify creds + connectivity. Cheap call, used at startup."""

    # === MARKET DATA (no auth needed for reads) ===

    @abstractmethod
    async def get_ticker(self, pair: str = "BTC/AED") -> Ticker:
        """Current bid/ask/last for the pair. Caches OK for ~30s."""

    @abstractmethod
    async def get_fee_schedule(self, pair: str = "BTC/AED") -> FeeSchedule:
        """Maker/taker fees + withdrawal fee. May be hardcoded if exchange doesn't expose."""

    async def get_order_minimum(self, pair: str = "BTC/AED") -> OrderMinimum:
        """Minimum order size for the pair on this exchange.

        Smart router uses this to exclude routes whose floor exceeds the
        cycle's notional. Adapters override to return live limits from the
        exchange API (e.g. ccxt market.limits) or empirically-verified
        constants. The base default returns "unknown" — meaning the router
        will not exclude this venue on min-size grounds.
        """
        base, quote = pair.split("/")
        return OrderMinimum(
            exchange=self.name, pair=pair,
            min_base=None, min_quote=None,
            quote_currency=quote, source="unknown",
        )

    # === ACCOUNT (auth required) ===

    @abstractmethod
    async def get_balances(self) -> list[Balance]:
        """All non-zero balances for the authenticated account."""

    async def get_balance(self, asset: str) -> Optional[Balance]:
        """Helper: balance for a specific asset. Default impl filters get_balances()."""
        balances = await self.get_balances()
        for b in balances:
            if b.asset.upper() == asset.upper():
                return b
        return None

    # === TRADING ===

    @abstractmethod
    async def place_market_buy(
        self,
        pair: str,
        quote_amount: Decimal,    # how much AED/USDT to spend
    ) -> Order:
        """Place a market buy. Returns Order; check status to confirm fill."""

    async def place_limit_buy(
        self,
        pair: str,
        quote_amount: Decimal,
        limit_price: Decimal,
    ) -> Order:
        """Place a limit buy at `limit_price` for `quote_amount` of quote currency.

        Adapter is responsible for converting quote_amount to base units at the
        limit price if the exchange's API takes base amounts. Returns an Order
        in PENDING status; caller polls `get_order` (or uses `wait_for_fill`).

        Default implementation raises — adapters that support limit orders
        must override.
        """
        raise NotImplementedError(f"{self.name} adapter does not implement place_limit_buy")

    async def cancel_order(self, pair: str, order_id: str) -> Order:
        """Cancel an open order. Returns the final Order state.

        Default implementation raises — override per adapter.
        """
        raise NotImplementedError(f"{self.name} adapter does not implement cancel_order")

    async def cancel_all_open_orders(self, pair: str) -> int:
        """Cancel every BOT-PLACED open order on `pair`. Returns count.

        Critical: this is called at the start of every cycle to clean up
        stale maker_fallback leftovers. It must NOT cancel orders the
        customer placed manually through the exchange's own UI alongside
        the bot. We distinguish by `clientOrderId` — bot orders are
        tagged with `BOT_CLORD_PREFIX`; manual orders have an exchange-
        generated id without the prefix.

        Adapters can override with a bulk-cancel endpoint if available.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            client = getattr(self, "_client", None)
            if client is None:
                return 0
            open_orders = await client.fetch_open_orders(pair)
        except Exception as e:
            # Can't LIST → can't know whether a stale bot order is resting.
            # Previously silent (return 0) — indistinguishable from "all
            # clear". Pairs a venue simply doesn't list land here every
            # cycle, so warning (not raising) is the right volume
            # (audit 2026-06-10 P2).
            _log.warning(
                "%s pre-cycle sweep could not list open orders for %s: %s",
                self.name, pair, e,
            )
            return 0
        n = 0
        failures: list[str] = []
        for o in open_orders or []:
            oid = o.get("id")
            if not oid:
                continue
            cid = (o.get("clientOrderId") or "") or (o.get("info", {}).get("clOrdId", ""))
            if not str(cid).startswith(BOT_CLORD_PREFIX):
                # Manual / pre-existing order — leave it alone.
                continue
            try:
                await self.cancel_order(pair, str(oid))
                n += 1
            except Exception as e:  # noqa: BLE001
                # A KNOWN stale bot order we failed to cancel can fill on
                # top of the new cycle's buy — that's a double-spend risk,
                # not a hiccup. Collect and surface (audit 2026-06-10 P2).
                failures.append(f"{oid}: {e}")
        if failures:
            raise ExchangeError(
                f"{self.name} sweep on {pair}: canceled {n} but FAILED to "
                f"cancel {len(failures)} stale bot order(s) — they may fill "
                f"on top of this cycle's buy. First error: {failures[0]}"
            )
        return n

    async def wait_for_fill(
        self,
        pair: str,
        order_id: str,
        timeout_seconds: float = 600,
        poll_interval_seconds: float = 5,
    ) -> Order:
        """Poll `get_order` until status is FILLED / CANCELLED / REJECTED / timeout.

        Returns the most recent Order. Does NOT cancel on timeout — caller
        decides whether to cancel + retry + market-buy fallback.
        """
        import asyncio as _asyncio
        deadline = _asyncio.get_event_loop().time() + timeout_seconds
        last: Optional[Order] = None
        while _asyncio.get_event_loop().time() < deadline:
            last = await self.get_order(pair, order_id)
            if last.status in (
                OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED
            ):
                return last
            await _asyncio.sleep(poll_interval_seconds)
        # Timed out — return whatever we last saw (still pending).
        if last is None:
            last = await self.get_order(pair, order_id)
        return last

    @abstractmethod
    async def get_order(self, pair: str, order_id: str) -> Order:
        """Refresh order state — used to confirm fills after placing."""

    async def _poll_until_settled(
        self,
        pair: str,
        placed: Order,
        max_seconds: int = 15,
    ) -> Order:
        """Wait for a freshly-placed market order to actually settle.

        Many exchanges return from `create_market_buy_order` with the order
        in `pending` state and `filled=0`. If the caller threads that 0 into
        a multi-hop next leg, the next hop's precision check rejects with a
        misleading "amount below minimum precision" error. This helper
        polls `get_order` once per second until status is FILLED/CANCELLED
        or we hit the timeout. Always returns the freshest snapshot.

        Adapters opt-in by calling this from their `place_market_buy` right
        before returning. Each adapter still handles its own error
        translation (InsufficientBalance, etc.) — this only deals with the
        "ordered → fill" race.
        """
        import asyncio
        from bitcoiners_dca.core.models import OrderStatus
        if not placed.order_id:
            return placed
        if placed.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return placed
        snap = placed
        for _ in range(max_seconds):
            await asyncio.sleep(1)
            try:
                snap = await self.get_order(pair, placed.order_id)
            except Exception:
                continue
            if snap.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                break
        return snap

    @abstractmethod
    async def get_trade_history(
        self,
        pair: str = "BTC/AED",
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Order]:
        """Filled orders, for reporting + reconciliation."""

    # === WITHDRAWALS (optional but standard) ===

    @abstractmethod
    async def withdraw_btc(
        self,
        amount_btc: Decimal,
        address: str,
        network: str = "bitcoin",  # or "lightning" for some exchanges
        rcvr_info: Optional[dict] = None,
    ) -> Withdrawal:
        """Withdraw BTC to an external address.

        `rcvr_info` is a dict carrying Travel Rule recipient info that
        some exchanges (notably OKX in regulated regions like UAE) demand
        per local KYC/AML rules. Shape expected by OKX:
          {"walletType": "private",
           "rcvrFirstName": "...", "rcvrLastName": "...",
           "rcvrCountry": "AE", "rcvrCountrySubDivision": "Dubai"}
        Exchanges that don't require it ignore the kwarg.

        The DCA bot's auto-withdraw uses this. Implementations should:
        - Check that the address is whitelisted (per-exchange policy)
        - Use the cheapest network available
        - Return promptly with PENDING status; caller polls if needed
        """

    @abstractmethod
    async def get_withdrawal(self, withdrawal_id: str) -> Withdrawal:
        """Refresh withdrawal state to confirm completion."""

    # === LIFECYCLE ===

    async def close(self) -> None:
        """Cleanup connections. Default is no-op; override if needed."""
        return None

    # === DRY-RUN SUPPORT ===

    dry_run: bool = False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} dry_run={self.dry_run}>"
