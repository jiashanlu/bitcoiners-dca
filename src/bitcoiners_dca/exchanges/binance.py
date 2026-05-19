"""
Binance adapter — uses ccxt against the global `binance.com` endpoint.

UAE STATUS:
  Since Jan 5, 2026, Binance services UAE customers through binance.com under
  its ADGM-licensed entities (not a separate `binance.ae` domain — that
  hostname has no DNS records). UAE residents who KYC under Binance ADGM use
  the standard binance.com API. ccxt's default `binance` driver hits the
  right endpoints automatically.

AED PAIRS:
  binance.com does NOT list any BTC/AED pair as of May 2026. UAE users who
  want to DCA into BTC via Binance must do it through BTC/USDT (or BTC/USDC)
  and convert AED↔USDT separately (Binance Convert, P2P, or external).
  For single-step AED-to-BTC DCA, use OKX or BitOasis. This adapter is kept
  for users who already hold USDT on Binance, or who want a USDT-quote leg
  alongside their AED-pair DCA. Configure `strategy.pair: BTC/USDT` if so.

  If a user enables Binance with `strategy.pair: BTC/AED`, the ticker fetch
  fails — the smart router catches the exception and silently drops Binance
  from the candidate set for that cycle.

The legacy `use_uae_endpoint` constructor argument is a no-op kept for
backward compatibility with older config files. We always use binance.com.
"""
from __future__ import annotations
import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import ccxt.async_support as ccxt_async
from tenacity import retry, stop_after_attempt, wait_exponential

from bitcoiners_dca.core.models import (
    Ticker, Balance, Order, OrderMinimum, OrderSide, OrderStatus, OrderType,
    Withdrawal, WithdrawalStatus, FeeSchedule,
)
from bitcoiners_dca.exchanges.base import (
    Exchange, ExchangeError, InsufficientBalanceError, WithdrawalDeniedError,
    make_bot_client_order_id,
    split_fee_by_currency as _split_fee_by_currency,
)

logger = logging.getLogger(__name__)


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


class BinanceExchange(Exchange):
    name = "binance"
    quote_currency = "USDT"  # no BTC/AED pair on binance.com — UAE users use USDT

    def __init__(self, api_key: str, api_secret: str, dry_run: bool = False,
                 use_uae_endpoint: bool = False):
        self.dry_run = dry_run
        if use_uae_endpoint:
            logger.warning(
                "Binance: use_uae_endpoint is deprecated and ignored. "
                "Binance services UAE users via binance.com under ADGM licensing."
            )
        self._client = ccxt_async.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def health_check(self) -> bool:
        try:
            await self._client.load_markets()
            await self._client.fetch_balance()
            return True
        except ccxt_async.AuthenticationError as e:
            raise ExchangeError(f"Binance authentication failed: {e}") from e
        except Exception as e:
            raise ExchangeError(f"Binance health check failed: {e}") from e

    async def get_ticker(self, pair: str = "BTC/USDT") -> Ticker:
        raw = await self._client.fetch_ticker(pair)
        return Ticker.from_prices(
            exchange=self.name,
            pair=pair,
            bid=_to_decimal(raw.get("bid")),
            ask=_to_decimal(raw.get("ask")),
            last=_to_decimal(raw.get("last")),
            ts=datetime.fromtimestamp(raw.get("timestamp", 0) / 1000, tz=timezone.utc),
        )

    async def get_fee_schedule(self, pair: str = "BTC/USDT") -> FeeSchedule:
        markets = await self._client.load_markets()
        market = markets.get(pair, {})
        return FeeSchedule(
            exchange=self.name,
            pair=pair,
            maker_pct=_to_decimal(market.get("maker", 0.001)),
            taker_pct=_to_decimal(market.get("taker", 0.001)),
            withdrawal_fee_btc=Decimal("0.0002"),  # standard Binance BTC withdrawal fee
        )

    async def get_order_minimum(self, pair: str = "BTC/USDT") -> OrderMinimum:
        # ccxt's market.limits exposes both axes:
        #   amount.min — base-currency floor (e.g. 0.00001 BTC for BTC/USDT)
        #   cost.min   — notional floor in quote ccy (e.g. 5 USDT for BTC/USDT)
        # We surface both and let the router pick the binding one at exec time.
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
        for asset, val in raw.get("total", {}).items():
            if val and Decimal(str(val)) > 0:
                out.append(Balance(
                    exchange=self.name,
                    asset=asset,
                    free=_to_decimal(raw["free"].get(asset)),
                    used=_to_decimal(raw["used"].get(asset)),
                    total=_to_decimal(raw["total"].get(asset)),
                ))
        return out

    async def place_market_buy(self, pair: str, quote_amount: Decimal) -> Order:
        if self.dry_run:
            ticker = await self.get_ticker(pair)
            return Order(
                exchange=self.name, order_id=f"dry-{datetime.now(timezone.utc).isoformat()}",
                pair=pair, side=OrderSide.BUY, type=OrderType.MARKET,
                amount_quote=quote_amount, amount_base=quote_amount / ticker.ask,
                price_filled_avg=ticker.ask, fee_quote=quote_amount * Decimal("0.001"),
                status=OrderStatus.FILLED, created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc),
            )
        # Binance market-buy: tell ccxt explicitly that we're spending
        # `quote_amount` of the quote currency (USDT/AED), not buying that
        # many units of the base. The previous code passed `amount=
        # quote_amount` AND `params={"quoteOrderQty": ...}` which was
        # ambiguous — depending on ccxt version, `amount` could be treated
        # as base-amount and the order would buy wildly more (or less) than
        # intended. `create_market_buy_order_with_cost` is ccxt's documented
        # entry point for "spend this much quote" and forwards
        # quoteOrderQty under the hood.
        try:
            raw = await self._client.create_market_buy_order_with_cost(
                symbol=pair,
                cost=float(quote_amount),
                params={
                    # Tag for cancel_all_open_orders selectivity.
                    "newClientOrderId": make_bot_client_order_id(),
                },
            )
            order = self._normalize_order(raw, pair, quote_amount)
            # Same fill-race as OKX: ccxt returns before raw["filled"] is
            # populated. Poll get_order to capture the real amount_base
            # before threading to next hop.
            return await self._poll_until_settled(pair, order)
        except ccxt_async.InsufficientFunds as e:
            raise InsufficientBalanceError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"Binance market buy failed: {e}") from e

    async def place_limit_buy(self, pair: str, quote_amount: Decimal, limit_price: Decimal) -> Order:
        if self.dry_run:
            # Dry-run simulates happy path: limit fills at the limit price.
            now = datetime.now(timezone.utc)
            return Order(
                exchange=self.name, order_id=f"dry-limit-{now.isoformat()}",
                pair=pair, side=OrderSide.BUY, type=OrderType.LIMIT,
                amount_quote=quote_amount, amount_base=quote_amount / limit_price,
                price_filled_avg=limit_price,
                fee_quote=quote_amount * Decimal("0.00075"),  # maker fee
                status=OrderStatus.FILLED, created_at=now, filled_at=now,
            )
        try:
            base_amount = quote_amount / limit_price
            raw = await self._client.create_limit_buy_order(
                symbol=pair, amount=float(base_amount), price=float(limit_price),
                params={"newClientOrderId": make_bot_client_order_id()},
            )
            return self._normalize_order(raw, pair, quote_amount)
        except ccxt_async.InsufficientFunds as e:
            raise InsufficientBalanceError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"Binance limit buy failed: {e}") from e

    async def cancel_order(self, pair: str, order_id: str) -> Order:
        if self.dry_run:
            now = datetime.now(timezone.utc)
            return Order(
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
            raise ExchangeError(f"Binance cancel failed: {e}") from e

    async def get_order(self, pair: str, order_id: str) -> Order:
        raw = await self._client.fetch_order(order_id, pair)
        return self._normalize_order(raw, pair, _to_decimal(raw.get("cost")))

    async def get_trade_history(self, pair: str = "BTC/USDT",
                                since: Optional[datetime] = None, limit: int = 100) -> list[Order]:
        since_ms = int(since.timestamp() * 1000) if since else None
        raw = await self._client.fetch_my_trades(pair, since=since_ms, limit=limit)
        return [self._normalize_trade_as_order(t, pair) for t in raw]

    async def withdraw_btc(self, amount_btc: Decimal, address: str,
                           network: str = "bitcoin",
                           rcvr_info: Optional[dict] = None) -> Withdrawal:
        from bitcoiners_dca.core.lightning import is_lightning
        if is_lightning(address) or network.lower() in ("lightning", "ln", "bolt11"):
            raise WithdrawalDeniedError(
                "Binance UAE does not support Lightning withdrawals — on-chain only. "
                "Use OKX for Lightning withdrawals."
            )

        if self.dry_run:
            return Withdrawal(
                exchange=self.name, withdrawal_id=f"dry-w-{datetime.now(timezone.utc).isoformat()}",
                asset="BTC", amount=amount_btc, address=address, fee=Decimal("0.0002"),
                status=WithdrawalStatus.PENDING, created_at=datetime.now(timezone.utc),
            )

        binance_network = "BTC" if network == "bitcoin" else network

        # UAE local entity (ADGM) requires the Travel Rule questionnaire
        # — calling /sapi/v1/capital/withdraw/apply directly returns
        # -4104 "Travel rule restrictions". Probe questionnaire-requirements
        # first; if it returns a non-empty payload, route through
        # /sapi/v1/localentity/withdraw/apply with the questionnaire JSON.
        try:
            requirements = await self._fetch_questionnaire_requirements(
                coin="BTC", network=binance_network,
                address=address, amount=str(amount_btc),
            )
        except Exception as e:
            logger.warning("Binance questionnaire-requirements probe failed: %s", e)
            requirements = None

        if self._requirements_indicate_travel_rule(requirements):
            return await self._withdraw_via_localentity(
                amount_btc=amount_btc, address=address,
                network=binance_network, rcvr_info=rcvr_info,
            )

        try:
            params = {"network": binance_network}
            raw = await self._client.withdraw(
                code="BTC", amount=float(amount_btc), address=address, params=params,
            )
            return Withdrawal(
                exchange=self.name, withdrawal_id=str(raw.get("id") or ""),
                asset="BTC", amount=amount_btc, address=address,
                fee=Decimal("0.0002"),
                status=WithdrawalStatus.PENDING, created_at=datetime.now(timezone.utc),
                txid=raw.get("txid"),
            )
        except ccxt_async.PermissionDenied as e:
            # Fallback: if the standard endpoint rejects with the
            # Travel-Rule error, retry through localentity. Covers the
            # case where questionnaire-requirements was unreachable.
            msg = str(e)
            if "-4104" in msg or "travel rule" in msg.lower() or "travel-rule" in msg.lower():
                logger.info("Binance returned Travel-Rule restriction; retrying via localentity")
                return await self._withdraw_via_localentity(
                    amount_btc=amount_btc, address=address,
                    network=binance_network, rcvr_info=rcvr_info,
                )
            raise WithdrawalDeniedError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"Binance withdraw failed: {e}") from e

    async def _fetch_questionnaire_requirements(self, coin: str, network: str,
                                                address: str, amount: str) -> dict:
        """GET /sapi/v1/localentity/questionnaire-requirements.

        Returns the questionnaire shape the local entity wants for this
        specific destination, or an empty/NIL payload when Travel Rule
        does not apply (e.g. small amount, non-regulated jurisdiction).
        """
        params = {
            "coin": coin, "network": network,
            "address": address, "amount": amount,
        }
        method = getattr(self._client, "sapiGetLocalentityQuestionnaireRequirements", None)
        if method is not None:
            return await method(params)
        return await self._client.request(
            path="localentity/questionnaire-requirements",
            api="sapi", method="GET", params=params,
        )

    @staticmethod
    def _requirements_indicate_travel_rule(requirements) -> bool:
        # The endpoint returns NIL/empty when Travel Rule does not apply,
        # and a dict carrying questionnaire metadata when it does. Treat
        # any non-empty dict/list as "yes, route through localentity".
        if requirements is None:
            return False
        if isinstance(requirements, (dict, list)):
            return bool(requirements)
        return False

    async def _withdraw_via_localentity(self, amount_btc: Decimal, address: str,
                                        network: str,
                                        rcvr_info: Optional[dict]) -> Withdrawal:
        """POST /sapi/v1/localentity/withdraw/apply with the UAE questionnaire.

        UAE (ADGM) questionnaire fields per Binance docs:
          isAddressOwner: 1=send to myself, 2=send to another beneficiary
          sendTo:         1=private wallet, 2=another VASP
          bnfType / bnfName / country / city: required only when
                                              isAddressOwner=2
        """
        questionnaire = self._build_uae_questionnaire(rcvr_info)
        params = {
            "coin": "BTC",
            "network": network,
            "address": address,
            "amount": str(amount_btc),
            "questionnaire": json.dumps(questionnaire),
        }
        try:
            method = getattr(self._client, "sapiPostLocalentityWithdrawApply", None)
            if method is not None:
                raw = await method(params)
            else:
                # ccxt 4.5 doesn't know `localentity/withdraw/apply`, so its
                # generic sign() falls into the default sapi branch that
                # urlencode()s the body. Binance's withdraw endpoints expect
                # rawencode (no percent-escaping of the JSON questionnaire's
                # `{`/`"`/`:`), so the server-side HMAC re-computation fails
                # with -1022 "Signature for this request is not valid".
                # Sign + POST manually using the same convention ccxt uses
                # for `capital/withdraw/apply`.
                raw = await self._signed_post_sapi("localentity/withdraw/apply", params)
        except ccxt_async.PermissionDenied as e:
            raise WithdrawalDeniedError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"Binance localentity withdraw failed: {e}") from e

        return Withdrawal(
            exchange=self.name,
            withdrawal_id=str(raw.get("trId") or raw.get("id") or ""),
            asset="BTC", amount=amount_btc, address=address,
            fee=Decimal("0.0002"),
            status=WithdrawalStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )

    async def get_withdrawal_whitelist(self, coin: str = "BTC") -> list[dict]:
        """List addresses pre-whitelisted at Binance for `coin` withdrawals.

        GET /sapi/v1/capital/withdraw/address/list — Binance is the only
        UAE-supported exchange that exposes this surface (OKX + BitOasis
        return 404 on every variant we tried). Returns a list of
        {address, network, label} dicts so the dashboard can render
        a 'Whitelisted at Binance' picker.
        """
        try:
            method = getattr(self._client, "sapiGetCapitalWithdrawAddressList", None)
            if method is not None:
                raw = await method({})
            else:
                raw = await self._client.request(
                    path="capital/withdraw/address/list",
                    api="sapi", method="GET", params={},
                )
        except Exception as e:
            logger.warning("Binance whitelist fetch failed: %s", e)
            return []
        out: list[dict] = []
        for entry in raw or []:
            entry_coin = entry.get("coin") or entry.get("asset") or ""
            if coin and entry_coin and entry_coin.upper() != coin.upper():
                continue
            address = entry.get("address") or entry.get("addr") or ""
            if not address:
                continue
            out.append({
                "address": address,
                "network": (entry.get("network") or "bitcoin").lower(),
                "label": entry.get("name") or entry.get("addressTag") or None,
            })
        return out

    async def _signed_post_sapi(self, path: str, params: dict) -> dict:
        """POST a Binance sapi endpoint, signing the body with rawencode.

        Mirrors what ccxt's binance.sign() does for `capital/withdraw/apply`:
          - body = rawencode({timestamp, recvWindow, **params}) + '&signature=' + hmac
          - Content-Type: application/x-www-form-urlencoded
          - X-MBX-APIKEY header
        Use this for any sapi POST whose path isn't in ccxt's
        rawencode-allowlist (e.g. `localentity/withdraw/apply`).
        """
        c = self._client
        extended: dict = c.extend({"timestamp": c.nonce()}, params)
        recv_window = c.safe_integer(c.options, "recvWindow")
        if recv_window is not None:
            extended["recvWindow"] = recv_window
        query = c.rawencode(extended)
        signature = c.hmac(
            c.encode(query), c.encode(c.secret), hashlib.sha256
        )
        body = f"{query}&signature={signature}"
        url = c.urls["api"]["sapi"] + "/" + path
        headers = {
            "X-MBX-APIKEY": c.apiKey,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        # ccxt's fetch() handles the HTTP round-trip + JSON parse + error
        # mapping (raises PermissionDenied on Binance error codes, etc.)
        return await c.fetch(url, method="POST", headers=headers, body=body)

    @staticmethod
    def _build_uae_questionnaire(rcvr_info: Optional[dict]) -> dict:
        # Self-custody is the default — most users send to their own
        # hardware wallet. Caller can flip via addressOwnerSelf=False in
        # rcvr_info to declare a third-party private-wallet recipient.
        rcvr_info = rcvr_info or {}
        is_self = bool(rcvr_info.get("addressOwnerSelf", True))
        q: dict = {
            "isAddressOwner": 1 if is_self else 2,
            "sendTo": 1,  # private wallet (we already block VASP routes)
        }
        if not is_self:
            first = (rcvr_info.get("rcvrFirstName") or "").strip()
            last = (rcvr_info.get("rcvrLastName") or "").strip()
            q["bnfType"] = 0  # individual
            q["bnfName"] = (f"{first} {last}").strip() or "Beneficiary"
            country = (rcvr_info.get("rcvrCountry") or "AE")
            q["country"] = country.lower()[:2]
            q["city"] = (rcvr_info.get("rcvrCountrySubDivision") or "Dubai").strip()
        return q

    async def get_withdrawal(self, withdrawal_id: str) -> Withdrawal:
        raw_list = await self._client.fetch_withdrawals(code="BTC", limit=50)
        for w in raw_list:
            if str(w.get("id")) == withdrawal_id:
                status_map = {
                    0: WithdrawalStatus.PENDING,  # email sent
                    1: WithdrawalStatus.PROCESSING,
                    6: WithdrawalStatus.COMPLETE,
                }
                return Withdrawal(
                    exchange=self.name, withdrawal_id=withdrawal_id,
                    asset="BTC", amount=_to_decimal(w.get("amount")),
                    address=w.get("address", ""),
                    fee=Decimal("0.0002"),
                    status=status_map.get(w.get("status"), WithdrawalStatus.PENDING),
                    txid=w.get("txid"),
                    created_at=datetime.fromtimestamp(w.get("timestamp", 0) / 1000, tz=timezone.utc),
                )
        raise ExchangeError(f"Binance withdrawal {withdrawal_id} not found")

    def _normalize_order(self, raw: dict, pair: str, quote_amount: Decimal) -> Order:
        status_map = {
            "open": OrderStatus.PENDING, "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED, "cancelled": OrderStatus.CANCELLED,
        }
        fee_base, fee_quote = _split_fee_by_currency(raw.get("fee"), pair)
        return Order(
            exchange=self.name, order_id=str(raw.get("id") or ""), pair=pair,
            side=OrderSide(raw.get("side", "buy")), type=OrderType(raw.get("type", "market")),
            amount_quote=quote_amount, amount_base=_to_decimal(raw.get("filled")),
            price_filled_avg=_to_decimal(raw.get("average") or raw.get("price")),
            fee_base=fee_base, fee_quote=fee_quote,
            status=status_map.get(raw.get("status", "open"), OrderStatus.PENDING),
            created_at=datetime.fromtimestamp(raw.get("timestamp", 0) / 1000, tz=timezone.utc) if raw.get("timestamp") else datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc) if raw.get("status") == "closed" else None,
        )

    def _normalize_trade_as_order(self, trade: dict, pair: str) -> Order:
        fee_base, fee_quote = _split_fee_by_currency(trade.get("fee"), pair)
        return Order(
            exchange=self.name, order_id=str(trade.get("order") or trade.get("id") or ""),
            pair=pair, side=OrderSide(trade.get("side", "buy")),
            type=OrderType.MARKET,
            amount_quote=_to_decimal(trade.get("cost")), amount_base=_to_decimal(trade.get("amount")),
            price_filled_avg=_to_decimal(trade.get("price")),
            fee_base=fee_base, fee_quote=fee_quote,
            status=OrderStatus.FILLED,
            created_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000, tz=timezone.utc),
            filled_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000, tz=timezone.utc),
        )

    async def close(self) -> None:
        await self._client.close()
