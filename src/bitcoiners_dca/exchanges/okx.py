"""
OKX adapter — uses the `ccxt` library which provides a maintained,
unified interface to OKX's V5 API.

OKX UAE-licensed entity (VARA registration) is the canonical OKX endpoint
for UAE residents. AED pairs are direct (BTC/AED is supported).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import ccxt.async_support as ccxt_async
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


# Errors that are safe to retry without risking a duplicate order. An
# order-placement method that succeeded server-side but raised
# client-side (transient parse/connection hiccup) MUST NOT be retried
# — re-entering the function would place a second real order. So we
# whitelist only network-level errors that fire BEFORE the request hits
# the exchange.
_SAFE_RETRY_EXCEPTIONS = (
    ccxt_async.NetworkError,
    ccxt_async.RequestTimeout,
    ccxt_async.DDoSProtection,
    ccxt_async.RateLimitExceeded,
)

logger = logging.getLogger(__name__)

from bitcoiners_dca.core.lightning import WithdrawalNetwork, detect_network
from bitcoiners_dca.core.models import (
    Ticker, Balance, Order, OrderMinimum, OrderSide, OrderStatus, OrderType,
    Withdrawal, WithdrawalStatus, FeeSchedule,
)
from bitcoiners_dca.exchanges.base import (
    Exchange, ExchangeError, InsufficientBalanceError, WithdrawalDeniedError,
    make_bot_client_order_id,
)


# OKX V5 chain identifiers — passed through ccxt `params["chain"]`
OKX_CHAIN_BITCOIN = "BTC-Bitcoin"
OKX_CHAIN_LIGHTNING = "BTC-Lightning"


def _to_decimal(value) -> Decimal:
    """Convert ccxt's float-or-string into Decimal cleanly."""
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


class OKXExchange(Exchange):
    name = "okx"

    def __init__(self, api_key: str, api_secret: str, passphrase: str,
                 dry_run: bool = False):
        self.dry_run = dry_run
        self._client = ccxt_async.okx({
            "apiKey": api_key,
            "secret": api_secret,
            "password": passphrase,  # OKX requires API passphrase
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        })

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def health_check(self) -> bool:
        try:
            await self._client.load_markets()
            # Auth check: try a private endpoint
            await self._client.fetch_balance()
            return True
        except ccxt_async.AuthenticationError as e:
            raise ExchangeError(f"OKX authentication failed: {e}") from e
        except Exception as e:
            raise ExchangeError(f"OKX health check failed: {e}") from e

    async def get_ticker(self, pair: str = "BTC/AED") -> Ticker:
        raw = await self._client.fetch_ticker(pair)
        return Ticker.from_prices(
            exchange=self.name,
            pair=pair,
            bid=_to_decimal(raw.get("bid")),
            ask=_to_decimal(raw.get("ask")),
            last=_to_decimal(raw.get("last")),
            ts=datetime.fromtimestamp(raw.get("timestamp", 0) / 1000, tz=timezone.utc),
        )

    async def get_fee_schedule(self, pair: str = "BTC/AED") -> FeeSchedule:
        markets = await self._client.load_markets()
        market = markets.get(pair, {})
        maker = market.get("maker", 0.001)
        taker = market.get("taker", 0.0015)
        # Withdrawal fee for BTC — fetch from currencies endpoint
        currencies = await self._client.fetch_currencies()
        btc = currencies.get("BTC", {})
        networks = btc.get("networks", {})
        # Prefer Lightning if available (much cheaper); fall back to BTC mainnet
        btc_network = networks.get("Bitcoin", {}) or networks.get("BTC", {})
        withdraw_fee = _to_decimal(btc_network.get("fee", 0.0002))
        return FeeSchedule(
            exchange=self.name,
            pair=pair,
            maker_pct=_to_decimal(maker),
            taker_pct=_to_decimal(taker),
            withdrawal_fee_btc=withdraw_fee,
        )

    async def get_order_minimum(self, pair: str = "BTC/AED") -> OrderMinimum:
        # OKX V5 publishes minSz (base) and minOrderSz; cost.min is usually
        # also populated for fiat pairs. ccxt normalizes both into
        # market["limits"]["amount"]["min"] and ...["cost"]["min"].
        markets = await self._client.load_markets()
        market = markets.get(pair) or {}
        limits = market.get("limits") or {}
        amt_min = (limits.get("amount") or {}).get("min")
        cost_min = (limits.get("cost") or {}).get("min")
        _, quote = pair.split("/")
        return OrderMinimum(
            exchange=self.name, pair=pair,
            min_base=_to_decimal(amt_min) if amt_min else None,
            min_quote=_to_decimal(cost_min) if cost_min else None,
            quote_currency=quote,
            source="api",
        )

    async def get_balances(self) -> list[Balance]:
        raw = await self._client.fetch_balance()
        out = []
        for asset, vals in raw.get("total", {}).items():
            if vals and Decimal(str(vals)) > 0:
                out.append(Balance(
                    exchange=self.name,
                    asset=asset,
                    free=_to_decimal(raw["free"].get(asset)),
                    used=_to_decimal(raw["used"].get(asset)),
                    total=_to_decimal(raw["total"].get(asset)),
                ))
        return out

    # Internal place-only path. ONLY retries the create_market_buy_order
    # call. `_poll_until_settled` MUST NOT be inside the @retry — a
    # network blip during the poll would re-enter and place a SECOND
    # real order. Critical safety boundary.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(_SAFE_RETRY_EXCEPTIONS),
        reraise=True,
    )
    async def _create_market_buy_only(
        self, pair: str, quote_amount: Decimal,
    ) -> Order:
        # `quoteOrderQty` + `tdMode=cash` — see place_market_buy docstring.
        params = {
            "quoteOrderQty": float(quote_amount),
            "tgtCcy": "quote_ccy",
            "tdMode": "cash",
            # Tag so cancel_all_open_orders can recognise bot orders
            # and leave the user's manual orders alone.
            "clOrdId": make_bot_client_order_id(),
        }
        logger.info(
            "OKX place_market_buy: pair=%s amount=%s params=%s",
            pair, float(quote_amount), params,
        )
        raw = await self._client.create_market_buy_order(
            symbol=pair,
            amount=float(quote_amount),  # ccxt expects float
            params=params,
        )
        return self._normalize_order(raw, pair, quote_amount)

    async def place_market_buy(self, pair: str, quote_amount: Decimal) -> Order:
        if self.dry_run:
            # Simulate the buy at current price
            ticker = await self.get_ticker(pair)
            fake_base = quote_amount / ticker.ask
            return Order(
                exchange=self.name,
                order_id=f"dry-{datetime.now(timezone.utc).isoformat()}",
                pair=pair,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                amount_quote=quote_amount,
                amount_base=fake_base,
                price_filled_avg=ticker.ask,
                fee_quote=quote_amount * Decimal("0.0015"),
                status=OrderStatus.FILLED,
                created_at=datetime.now(timezone.utc),
                filled_at=datetime.now(timezone.utc),
            )

        # OKX market-buy: pass `quoteOrderQty` for AED-based market buys.
        # `tdMode=cash` forces SPOT settlement — without it, accounts in
        # cross-margin or unified-account mode have OKX route the order
        # through the margin engine, which fails with 51008 "available
        # AED is insufficient, available margin (in USD) is too low for
        # borrowing" even when spot AED balance is plenty. Cash mode
        # only checks the spot wallet, which is what we want for DCA.
        try:
            order = await self._create_market_buy_only(pair, quote_amount)
        except ccxt_async.InsufficientFunds as e:
            raise InsufficientBalanceError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"OKX market buy failed: {e}") from e

        # OKX returns the order before it's been filled (raw["filled"]=0
        # at place-time). Use the base-class helper to poll for the real
        # fill — see `Exchange._poll_until_settled`. NOTE: this is
        # deliberately OUTSIDE the @retry block above so a network
        # timeout during the poll doesn't re-enter and place a second
        # real order. Worst case: we return the order with status=PENDING
        # and the strategy / next cycle reconciles.
        return await self._poll_until_settled(pair, order)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(_SAFE_RETRY_EXCEPTIONS),
        reraise=True,
    )
    async def place_limit_buy(
        self,
        pair: str,
        quote_amount: Decimal,
        limit_price: Decimal,
    ) -> Order:
        if self.dry_run:
            # Dry-run simulates the happy path: limit fills at the limit price.
            # We mark status FILLED so wait_for_fill returns immediately rather
            # than making a real get_order call against a non-existent order id.
            now = datetime.now(timezone.utc)
            base = quote_amount / limit_price
            return Order(
                exchange=self.name,
                order_id=f"dry-limit-{now.isoformat()}",
                pair=pair, side=OrderSide.BUY, type=OrderType.LIMIT,
                amount_quote=quote_amount, amount_base=base,
                price_filled_avg=limit_price,
                fee_quote=quote_amount * Decimal("0.001"),  # maker fee
                status=OrderStatus.FILLED,
                created_at=now, filled_at=now,
            )
        try:
            base_amount = quote_amount / limit_price
            logger.info(
                "OKX place_limit_buy: pair=%s quote_amount=%s limit_price=%s → base_amount=%s",
                pair, quote_amount, limit_price, base_amount,
            )
            raw = await self._client.create_limit_buy_order(
                symbol=pair, amount=float(base_amount), price=float(limit_price),
                # See place_market_buy: cash mode forces spot settlement so
                # OKX doesn't route through margin and reject for borrow-side
                # liquidity that DCA accounts don't have. Tag clOrdId so
                # cancel_all_open_orders only cleans up bot-placed orders.
                params={
                    "tdMode": "cash",
                    "clOrdId": make_bot_client_order_id(),
                },
            )
            return self._normalize_order(raw, pair, quote_amount)
        except ccxt_async.InsufficientFunds as e:
            raise InsufficientBalanceError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"OKX limit buy failed: {e}") from e

    async def cancel_order(self, pair: str, order_id: str) -> Order:
        if self.dry_run:
            # Dry-run cancel just flips status
            o = await self.get_order(pair, order_id) if order_id.startswith("dry") else None
            now = datetime.now(timezone.utc)
            return o or Order(
                exchange=self.name, order_id=order_id, pair=pair,
                side=OrderSide.BUY, type=OrderType.LIMIT,
                amount_quote=Decimal(0), amount_base=Decimal(0),
                price_filled_avg=Decimal(0), fee_quote=Decimal(0),
                status=OrderStatus.CANCELLED, created_at=now, filled_at=None,
            )
        try:
            await self._client.cancel_order(order_id, pair)
            return await self.get_order(pair, order_id)
        except Exception as e:
            raise ExchangeError(f"OKX cancel failed: {e}") from e

    async def get_order(self, pair: str, order_id: str) -> Order:
        raw = await self._client.fetch_order(order_id, pair)
        return self._normalize_order(raw, pair, _to_decimal(raw.get("cost")))

    async def get_trade_history(
        self,
        pair: str = "BTC/AED",
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Order]:
        since_ms = int(since.timestamp() * 1000) if since else None
        raw = await self._client.fetch_my_trades(pair, since=since_ms, limit=limit)
        # ccxt returns trades, not orders; group/normalize naively as Orders
        return [self._normalize_trade_as_order(t, pair) for t in raw]

    async def withdraw_btc(
        self,
        amount_btc: Decimal,
        address: str,
        network: str = "bitcoin",
    ) -> Withdrawal:
        """Withdraw BTC. Auto-detects Lightning when `address` is a BOLT11 invoice.

        Supported networks on OKX:
          - "bitcoin"    → on-chain (chain=BTC-Bitcoin)
          - "lightning"  → Lightning (chain=BTC-Lightning, address=BOLT11 invoice)

        For ad-hoc convenience, if you pass a BOLT11 invoice (lnbc…) without
        specifying `network`, this method switches to Lightning automatically.
        OKX does NOT support LNURL or Lightning Addresses — only raw invoices.
        """
        chain, normalized_network = self._resolve_chain(address, network)

        if self.dry_run:
            return Withdrawal(
                exchange=self.name,
                withdrawal_id=f"dry-w-{datetime.now(timezone.utc).isoformat()}",
                asset="BTC",
                amount=amount_btc,
                address=address,
                fee=Decimal("0") if normalized_network == "lightning" else Decimal("0.0002"),
                status=WithdrawalStatus.PENDING,
                created_at=datetime.now(timezone.utc),
            )
        try:
            params = {"chain": chain}
            raw = await self._client.withdraw(
                code="BTC",
                amount=float(amount_btc),
                address=address,
                params=params,
            )
            return Withdrawal(
                exchange=self.name,
                withdrawal_id=str(raw.get("id") or ""),
                asset="BTC",
                amount=amount_btc,
                address=address,
                fee=_to_decimal(raw.get("fee", {}).get("cost", "0.0002") if isinstance(raw.get("fee"), dict) else raw.get("fee", "0.0002")),
                status=WithdrawalStatus.PENDING,
                created_at=datetime.now(timezone.utc),
                txid=raw.get("txid"),
            )
        except ccxt_async.PermissionDenied as e:
            raise WithdrawalDeniedError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"OKX withdraw failed: {e}") from e

    @staticmethod
    def _resolve_chain(address: str, network: str) -> tuple[str, str]:
        """Pick the OKX chain identifier from `network` + address fingerprint.

        - When `network` is empty, auto-detect from the address.
        - When `network` is explicit ("bitcoin" or "lightning"), it must match
          the address fingerprint — mismatches raise WithdrawalDeniedError.
        Returns (okx_chain, normalized_network_name).
        """
        detected = detect_network(address)
        net = network.lower().strip()

        if not net:
            if detected == WithdrawalNetwork.LIGHTNING:
                return OKX_CHAIN_LIGHTNING, "lightning"
            if detected == WithdrawalNetwork.BITCOIN:
                return OKX_CHAIN_BITCOIN, "bitcoin"
            raise WithdrawalDeniedError(
                f"Cannot infer network from address (detected={detected.value}). "
                "Pass network='bitcoin' or 'lightning'."
            )

        if net in ("lightning", "ln", "bolt11"):
            if detected != WithdrawalNetwork.LIGHTNING:
                raise WithdrawalDeniedError(
                    "OKX Lightning withdrawals require a BOLT11 invoice (lnbc…). "
                    "LNURL and Lightning Addresses are not supported."
                )
            return OKX_CHAIN_LIGHTNING, "lightning"

        if net in ("bitcoin", "btc", "onchain"):
            if detected == WithdrawalNetwork.LIGHTNING:
                raise WithdrawalDeniedError(
                    "Address looks like a Lightning invoice, not on-chain Bitcoin."
                )
            if detected not in (WithdrawalNetwork.BITCOIN, WithdrawalNetwork.UNKNOWN):
                raise WithdrawalDeniedError(
                    f"Address looks like {detected.value}, not on-chain Bitcoin."
                )
            return OKX_CHAIN_BITCOIN, "bitcoin"

        raise WithdrawalDeniedError(f"Unsupported OKX network: {network}")

    async def get_withdrawal(self, withdrawal_id: str) -> Withdrawal:
        # ccxt: fetch_withdrawals returns recent; we filter by id
        raw_list = await self._client.fetch_withdrawals(code="BTC", limit=50)
        for w in raw_list:
            if str(w.get("id")) == withdrawal_id:
                status_map = {
                    "pending": WithdrawalStatus.PENDING,
                    "ok": WithdrawalStatus.COMPLETE,
                    "failed": WithdrawalStatus.FAILED,
                }
                return Withdrawal(
                    exchange=self.name,
                    withdrawal_id=withdrawal_id,
                    asset="BTC",
                    amount=_to_decimal(w.get("amount")),
                    address=w.get("address", ""),
                    fee=_to_decimal(w.get("fee", {}).get("cost") if isinstance(w.get("fee"), dict) else w.get("fee", 0)),
                    status=status_map.get(w.get("status", "pending"), WithdrawalStatus.PENDING),
                    txid=w.get("txid"),
                    created_at=datetime.fromtimestamp(w.get("timestamp", 0) / 1000, tz=timezone.utc),
                )
        raise ExchangeError(f"OKX withdrawal {withdrawal_id} not found")

    # === HELPERS ===

    def _normalize_order(self, raw: dict, pair: str, quote_amount: Decimal) -> Order:
        status_map = {
            "open": OrderStatus.PENDING,
            "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
        }
        return Order(
            exchange=self.name,
            order_id=str(raw.get("id") or ""),
            pair=pair,
            side=OrderSide(raw.get("side", "buy")),
            type=OrderType(raw.get("type", "market")),
            amount_quote=quote_amount,
            amount_base=_to_decimal(raw.get("filled")),
            price_filled_avg=_to_decimal(raw.get("average") or raw.get("price")),
            fee_quote=_to_decimal(raw.get("fee", {}).get("cost") if isinstance(raw.get("fee"), dict) else 0),
            status=status_map.get(raw.get("status", "open"), OrderStatus.PENDING),
            created_at=datetime.fromtimestamp(raw.get("timestamp", 0) / 1000, tz=timezone.utc) if raw.get("timestamp") else datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc) if raw.get("status") == "closed" else None,
        )

    def _normalize_trade_as_order(self, trade: dict, pair: str) -> Order:
        return Order(
            exchange=self.name,
            order_id=str(trade.get("order") or trade.get("id") or ""),
            pair=pair,
            side=OrderSide(trade.get("side", "buy")),
            type=OrderType.MARKET,
            amount_quote=_to_decimal(trade.get("cost")),
            amount_base=_to_decimal(trade.get("amount")),
            price_filled_avg=_to_decimal(trade.get("price")),
            fee_quote=_to_decimal(trade.get("fee", {}).get("cost") if isinstance(trade.get("fee"), dict) else 0),
            status=OrderStatus.FILLED,
            created_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000, tz=timezone.utc),
            filled_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000, tz=timezone.utc),
        )

    async def close(self) -> None:
        await self._client.close()
