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
    Ticker, Balance, Order, OrderStatus, Withdrawal, FeeSchedule
)


# Prefix every order's clientOrderId with this so the pre-cycle sweep can
# distinguish bot orders from orders the customer placed manually on the
# exchange's own UI. Keep short (OKX clOrdId max 32 chars, alphanumeric).
BOT_CLORD_PREFIX = "bdca"


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
        try:
            client = getattr(self, "_client", None)
            if client is None:
                return 0
            open_orders = await client.fetch_open_orders(pair)
        except Exception:
            return 0
        n = 0
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
            except Exception:
                # Individual cancel failures shouldn't abort the sweep — log
                # but continue. A re-running cycle picks up whatever's left.
                continue
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
    ) -> Withdrawal:
        """Withdraw BTC to an external address.

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
