"""
OKX adapter — uses the `ccxt` library which provides a maintained,
unified interface to OKX's V5 API.

OKX UAE-licensed entity (VARA registration) is the canonical OKX endpoint
for UAE residents. AED pairs are direct (BTC/AED is supported).
"""
from __future__ import annotations
import asyncio
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
    make_bot_client_order_id, resolve_partial_status,
    split_fee_by_currency as _split_fee_by_currency,
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
    supports_lightning_withdrawal = True  # OKX accepts BOLT11 invoices

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

    # reraise=True: surface the REAL final error, not tenacity's
    # RetryError[<Future>] wrapper — the wrapper broke error classification
    # and user-facing messages (audit 2026-06-10 P3).
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
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
        maker = _to_decimal(market.get("maker", 0.001))
        taker = _to_decimal(market.get("taker", 0.0015))
        # ccxt's load_markets returns the account's standard spot-tier fees
        # (typically 0.08% maker / 0.10% taker at L1) for every market. That
        # is correct for stablecoin-quoted pairs but WRONG for OKX's AED-
        # quoted fiat market, which has its own (much higher) fee schedule
        # — ~0.40% maker, ~0.60% taker. Without this floor the smart
        # router systematically under-prices AED-leg routes vs
        # stablecoin-leg routes and biases toward AED-direct.
        #
        # Confirmed via live cycle data on the benbois prod tenant
        # 2026-05-25: BTC/AED taker fill = 0.584% effective, BTC/AED
        # passive limit fill = 0.400% effective.
        #
        # Use `max(...)` — never go BELOW ccxt's value, in case OKX ever
        # surfaces the actual AED-tier fees through the markets endpoint.
        if (pair.split("/")[1] if "/" in pair else "").upper() == "AED":
            maker = max(maker, Decimal("0.0040"))
            taker = max(taker, Decimal("0.0060"))
        # Withdrawal fee for BTC — fetch from currencies endpoint.
        currencies = await self._client.fetch_currencies()
        btc = currencies.get("BTC", {})
        networks = btc.get("networks", {})
        # Prefer the Lightning network's fee when OKX exposes it (near-zero),
        # else fall back to BTC mainnet. The old code claimed to prefer
        # Lightning in a comment but only ever read the mainnet fee, so the
        # schedule overstated Lightning withdrawal cost (audit 2026-06-02 P3).
        ln_network = (
            networks.get("Lightning", {})
            or networks.get("LN", {})
            or networks.get("BTC-Lightning", {})
        )
        if ln_network and ln_network.get("fee") is not None:
            withdraw_fee = _to_decimal(ln_network.get("fee"))
        else:
            btc_network = networks.get("Bitcoin", {}) or networks.get("BTC", {})
            withdraw_fee = _to_decimal(btc_network.get("fee", 0.0002))
        return FeeSchedule(
            exchange=self.name,
            pair=pair,
            maker_pct=maker,
            taker_pct=taker,
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

    async def _order_for_client_id(self, pair: str, cl_ord_id: str):
        """Look up an order by our clOrdId. Returns the raw ccxt order dict
        if it exists, False if confirmed absent, None if the lookup itself
        failed (state unknown)."""
        try:
            raw = await self._client.fetch_order(
                None, pair, params={"clOrdId": cl_ord_id}
            )
            return raw if raw else False
        except ccxt_async.OrderNotFound:
            return False
        except Exception as e:  # noqa: BLE001 — any failure means "unknown"
            logger.warning(
                "OKX clOrdId lookup failed for %s: %s", cl_ord_id, e
            )
            return None

    async def _place_idempotent(
        self, pair: str, quote_amount: Decimal, cl_ord_id: str, place,
    ) -> Order:
        """Place an order with retries that can never double-buy.

        `place` is an async callable that performs the actual create call.
        Only _SAFE_RETRY_EXCEPTIONS are retried, and before EVERY re-attempt
        we look the clOrdId up server-side:

          - A network error on placement does not mean the order failed —
            it may have landed and filled. A MARKET order fills instantly,
            so OKX's duplicate-clOrdId rejection (which only guards while
            the original order is still live) does NOT stop a re-placement
            from buying twice (audit 2026-06-10 P1). The previous blind
            tenacity @retry relied exactly on that rejection.
          - If the lookup finds the order, return it — never re-place.
          - If the lookup itself fails, the state is UNKNOWN: refuse to
            re-place and surface an error. A failed cycle beats a
            double-buy; the next cycle's sweep reconciles.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
                existing = await self._order_for_client_id(pair, cl_ord_id)
                if existing is None:
                    raise ExchangeError(
                        f"OKX order state unknown after network error "
                        f"(clOrdId={cl_ord_id}) — not re-placing to avoid a "
                        f"double-buy"
                    ) from last_exc
                if existing is not False:
                    logger.warning(
                        "OKX retry: clOrdId=%s already exists server-side — "
                        "returning it instead of re-placing", cl_ord_id,
                    )
                    return self._normalize_order(existing, pair, quote_amount)
            try:
                raw = await place()
                return self._normalize_order(raw, pair, quote_amount)
            except _SAFE_RETRY_EXCEPTIONS as e:
                last_exc = e
                logger.warning(
                    "OKX placement attempt %d failed (%s) — will verify "
                    "before retrying", attempt + 1, e,
                )
        raise last_exc  # type: ignore[misc]  # loop always sets it before exit

    # Internal place-only path. ONLY retries the create_market_buy_order
    # call. `_poll_until_settled` MUST NOT be inside the retry — a
    # network blip during the poll would re-enter and place a SECOND
    # real order. Critical safety boundary.
    #
    # IDEMPOTENCY: the caller (place_market_buy) generates the clOrdId
    # ONCE and passes it in here (audit 2026-05-21). Re-attempts go
    # through _place_idempotent, which verifies the clOrdId server-side
    # before ever re-placing (audit 2026-06-10).
    async def _create_market_buy_only(
        self, pair: str, quote_amount: Decimal, cl_ord_id: str,
    ) -> Order:
        # `quoteOrderQty` + `tdMode=cash` — see place_market_buy docstring.
        params = {
            "quoteOrderQty": float(quote_amount),
            "tgtCcy": "quote_ccy",
            "tdMode": "cash",
            # Tag so cancel_all_open_orders can recognise bot orders
            # and leave the user's manual orders alone. Stable across
            # retries — see method docstring.
            "clOrdId": cl_ord_id,
        }

        async def _place():
            logger.info(
                "OKX place_market_buy: pair=%s amount=%s params=%s",
                pair, float(quote_amount), params,
            )
            return await self._client.create_market_buy_order(
                symbol=pair,
                amount=float(quote_amount),  # ccxt expects float
                params=params,
            )

        return await self._place_idempotent(pair, quote_amount, cl_ord_id, _place)

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
                # AED pairs carry OKX's ~0.6% taker, not the 0.15% USDT-pair
                # rate — hardcoding 0.0015 understated the dry-run/preview AED
                # fee ~4x (audit 2026-06-02 P3).
                fee_quote=quote_amount * (
                    Decimal("0.006") if pair.upper().endswith("/AED")
                    else Decimal("0.0015")
                ),
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
        #
        # Generate the clOrdId ONCE here — every retry inside
        # _create_market_buy_only reuses it so OKX's server-side
        # duplicate-clOrdId rejection prevents double-fills on network
        # blips. Audit P0 2026-05-21.
        cl_ord_id = make_bot_client_order_id()
        try:
            order = await self._create_market_buy_only(pair, quote_amount, cl_ord_id)
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

    # Same idempotency story as _create_market_buy_only above: the outer
    # wrapper generates clOrdId once (audit 2026-05-21); re-attempts verify
    # the clOrdId server-side via _place_idempotent before re-placing
    # (audit 2026-06-10 — a partially/instantly filled limit order is no
    # longer live, so the duplicate-clOrdId rejection can't be relied on).
    async def _create_limit_buy_only(
        self,
        pair: str,
        quote_amount: Decimal,
        limit_price: Decimal,
        cl_ord_id: str,
    ) -> Order:
        base_amount = quote_amount / limit_price

        async def _place():
            logger.info(
                "OKX place_limit_buy: pair=%s quote_amount=%s limit_price=%s → base_amount=%s",
                pair, quote_amount, limit_price, base_amount,
            )
            return await self._client.create_limit_buy_order(
                symbol=pair, amount=float(base_amount), price=float(limit_price),
                # See place_market_buy: cash mode forces spot settlement so
                # OKX doesn't route through margin and reject for borrow-side
                # liquidity that DCA accounts don't have. clOrdId is stable
                # across retries — caller generates it once.
                params={
                    "tdMode": "cash",
                    "clOrdId": cl_ord_id,
                },
            )

        return await self._place_idempotent(pair, quote_amount, cl_ord_id, _place)

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
                fee_quote=quote_amount * (
                    Decimal("0.004") if pair.upper().endswith("/AED")
                    else Decimal("0.001")
                ),  # maker fee — AED tier is ~0.4%, not 0.1% (audit P3)
                status=OrderStatus.FILLED,
                created_at=now, filled_at=now,
            )
        # Generate clOrdId ONCE before the @retry-wrapped call. See OKX
        # _create_market_buy_only docstring for the rationale.
        cl_ord_id = make_bot_client_order_id()
        try:
            return await self._create_limit_buy_only(
                pair, quote_amount, limit_price, cl_ord_id,
            )
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
        rcvr_info: Optional[dict] = None,
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
        # ccxt's okx.withdraw() requires `fee` in params or it errors:
        # "okx withdraw() requires a 'fee' string parameter". OKX charges
        # 0 for Lightning. For on-chain, fee is dynamic; query the
        # exchange's current network fee for BTC-Bitcoin and pass that.
        # Falls back to a conservative default if the lookup fails so
        # the call doesn't hard-error on a transient network blip.
        if normalized_network == "lightning":
            fee_str = "0"
        else:
            fee_str = await self._fetch_btc_onchain_fee_str()

        # OKX has separate Trading + Funding accounts; the withdrawal
        # endpoint pulls from Funding only. DCA buys settle in Trading,
        # so without an auto-transfer here you'd see error 58350
        # "Insufficient balance" on every withdrawal even though the
        # combined balance is fine. Move the shortfall over.
        needed = amount_btc + _to_decimal(fee_str)
        try:
            await self._ensure_funding_balance("BTC", needed)
        except ExchangeError:
            raise
        except Exception as e:
            raise ExchangeError(
                f"OKX withdraw failed before submission: couldn't move BTC from "
                f"Trading to Funding ({e})"
            ) from e

        try:
            params: dict = {"chain": chain, "fee": fee_str}
            # OKX-in-UAE (and other regulated regions) require Travel
            # Rule recipient info per FATF rules. ccxt forwards `params`
            # as part of the request body, which it JSON-encodes itself.
            # Passing the rcvr_info dict directly lets ccxt nest it as a
            # JSON object under rcvrInfo. We tried json.dumps()-ing it
            # first and OKX rejected with code 50002 "JSON syntax error"
            # because the rcvrInfo value showed up as a quoted string
            # rather than a nested object.
            # Skipping the param entirely triggers OKX error 58237.
            if rcvr_info:
                params["rcvrInfo"] = rcvr_info
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

    async def _ensure_funding_balance(self, ccy: str, needed: Decimal) -> None:
        """Make the Funding account hold at least `needed` of `ccy`.

        OKX splits balances across Trading + Funding sub-accounts. The
        withdrawal endpoint only pulls from Funding, but everything the
        bot buys lands in Trading. Without this auto-transfer, every
        withdrawal hits 58350 "Insufficient balance" the first time it
        runs against a freshly-bought balance.

        We check Funding's `free` balance and, if short, transfer the
        shortfall from Trading. If Trading + Funding combined still
        can't cover it, the subsequent withdraw call will fail loudly
        with the real shortfall — same as the previous behaviour, just
        with a clearer error context.
        """
        try:
            bal = await self._client.fetch_balance({"type": "funding"})
        except Exception as e:
            raise ExchangeError(f"couldn't read OKX Funding balance: {e}") from e
        funding_free = _to_decimal((bal.get(ccy) or {}).get("free") or 0)
        if funding_free >= needed:
            return
        shortfall = needed - funding_free
        # ccxt.okx.transfer maps "trading"→"6→18" etc. behind the scenes;
        # we use string codes so the adapter stays portable across ccxt
        # versions.
        try:
            await self._client.transfer(
                ccy, float(shortfall), "trading", "funding",
            )
        except Exception as e:
            raise ExchangeError(
                f"OKX Trading→Funding transfer of {shortfall} {ccy} failed: {e}"
            ) from e

    async def _fetch_btc_onchain_fee_str(self) -> str:
        """Look up OKX's current advertised on-chain BTC withdrawal fee.

        ccxt's `fetch_currencies()` returns per-network metadata including
        a `fee` field for each chain. We pull the `BTC-Bitcoin` network's
        fee; fall back to a conservative default if the lookup fails so
        a transient network blip doesn't block all on-chain withdrawals.
        OKX's standard on-chain BTC fee in 2026 is around 0.00005 BTC.
        """
        try:
            currencies = await self._client.fetch_currencies()
            btc = (currencies or {}).get("BTC") or {}
            networks = btc.get("networks") or {}
            # ccxt normalizes the network key; look for both "Bitcoin"
            # and the OKX raw form, since adapters disagree on the key.
            for key in ("Bitcoin", "BTC", "BTC-Bitcoin"):
                net = networks.get(key)
                if net and net.get("fee") is not None:
                    return str(net["fee"])
            # Some ccxt versions surface fee at the top level of the
            # currency dict rather than per-network.
            if btc.get("fee") is not None:
                return str(btc["fee"])
        except Exception:
            pass
        # Conservative default — typical OKX BTC-Bitcoin withdrawal fee.
        return "0.0001"

    @staticmethod
    def _resolve_chain(address: str, network: str) -> tuple[str, str]:
        """Pick the OKX chain identifier from `network` + address fingerprint.

        - When `network` is empty, auto-detect from the address.
        - When `network` is explicit ("bitcoin" or "lightning"), it must match
          the address fingerprint — mismatches raise WithdrawalDeniedError.
        Returns (okx_chain, normalized_network_name).
        """
        # OKX accepts BOLT11 invoices, raw Lightning Addresses (LUD-16),
        # and on-chain BTC addresses on their withdrawal endpoint. We
        # strip an optional `:label` suffix before fingerprinting so the
        # caller can pass `you@host:mylabel` for OKX's address-book
        # whitelist matching.
        addr_for_detect = address.split(":", 1)[0] if "@" in address else address
        detected = detect_network(addr_for_detect)
        net = network.lower().strip()

        if not net:
            if detected in (WithdrawalNetwork.LIGHTNING, WithdrawalNetwork.LIGHTNING_ADDRESS):
                return OKX_CHAIN_LIGHTNING, "lightning"
            if detected == WithdrawalNetwork.BITCOIN:
                return OKX_CHAIN_BITCOIN, "bitcoin"
            raise WithdrawalDeniedError(
                f"Cannot infer network from address (detected={detected.value}). "
                "Pass network='bitcoin' or 'lightning'."
            )

        if net in ("lightning", "ln", "bolt11"):
            if detected not in (WithdrawalNetwork.LIGHTNING, WithdrawalNetwork.LIGHTNING_ADDRESS):
                raise WithdrawalDeniedError(
                    "OKX Lightning withdrawals require a BOLT11 invoice (lnbc…) "
                    "or a Lightning Address (you@host)."
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
        fee_base, fee_quote = _split_fee_by_currency(raw.get("fee"), pair)
        status = status_map.get(raw.get("status", "open"), OrderStatus.PENDING)
        # A resting limit with filled>0 is a PARTIAL, not PENDING — without
        # this the maker fallback re-bought the full amount on top of the
        # partial fill (audit 2026-06-02 P0).
        status = resolve_partial_status(status, raw.get("filled"), raw.get("amount"))
        return Order(
            exchange=self.name,
            order_id=str(raw.get("id") or ""),
            pair=pair,
            side=OrderSide(raw.get("side", "buy")),
            type=OrderType(raw.get("type", "market")),
            amount_quote=quote_amount,
            amount_base=_to_decimal(raw.get("filled")),
            price_filled_avg=_to_decimal(raw.get("average") or raw.get("price")),
            fee_base=fee_base,
            fee_quote=fee_quote,
            status=status,
            created_at=datetime.fromtimestamp(raw.get("timestamp", 0) / 1000, tz=timezone.utc) if raw.get("timestamp") else datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc) if raw.get("status") == "closed" else None,
        )

    def _normalize_trade_as_order(self, trade: dict, pair: str) -> Order:
        fee_base, fee_quote = _split_fee_by_currency(trade.get("fee"), pair)
        return Order(
            exchange=self.name,
            order_id=str(trade.get("order") or trade.get("id") or ""),
            pair=pair,
            side=OrderSide(trade.get("side", "buy")),
            type=OrderType.MARKET,
            amount_quote=_to_decimal(trade.get("cost")),
            amount_base=_to_decimal(trade.get("amount")),
            price_filled_avg=_to_decimal(trade.get("price")),
            fee_base=fee_base,
            fee_quote=fee_quote,
            status=OrderStatus.FILLED,
            created_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000, tz=timezone.utc),
            filled_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000, tz=timezone.utc),
        )

    async def close(self) -> None:
        await self._client.close()
