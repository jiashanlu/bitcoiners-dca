"""
BitOasis adapter — production REST integration against the public API v1.

Specs verified against https://api.bitoasis.net/doc/ (BitOasis Public API 1.0):
- Base URL:    https://api.bitoasis.net/v1
- Auth:        Authorization: Bearer {API_TOKEN}   (single-token, no HMAC)
- Pair format: BTC-AED (hyphenated, BASE-QUOTE, uppercase)
- Order amt:   `amount` field is the BASE amount (BTC), not quote (AED)
- Statuses:    OPEN / DONE / CANCELED (orders), PENDING / PROCESSING / DONE (withdrawals)

The adapter mirrors the structure of the OKX adapter: async, retry-on-transient-
failure, dry-run-aware, structured errors, async context manager for cleanup.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bitcoiners_dca.core.models import (
    Balance,
    FeeSchedule,
    Order,
    OrderMinimum,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
    Withdrawal,
    WithdrawalStatus,
)
from bitcoiners_dca.exchanges.base import (
    Exchange,
    ExchangeError,
    InsufficientBalanceError,
    WithdrawalDeniedError,
)

logger = logging.getLogger(__name__)


BITOASIS_BASE_URL = "https://api.bitoasis.net/v1"

RETRY_ATTEMPTS = 3
RETRY_WAIT_MIN = 1
RETRY_WAIT_MAX = 8

# BitOasis Pro retail tier — fees aren't exposed via API, so use published values.
# Override via config if your account has different tier pricing.
DEFAULT_TAKER_PCT = Decimal("0.005")    # 0.5%
DEFAULT_MAKER_PCT = Decimal("0.002")    # 0.2%
DEFAULT_BTC_WITHDRAW_FEE = Decimal("0.0005")

# BitOasis minimum order size for BTC/AED is BTC-denominated.
# Empirically confirmed 2026-05-14 via a descending limit-order probe
# against /v1/exchange/order: AED-denominated 50/25/10 placed cleanly;
# AED 5/2/1 rejected with "Amount is too low! Minimum is 0.000048 BTC".
# BitOasis's public docs claim AED 50 — that figure is the implied
# minimum at their internal reference price and not what the API
# actually enforces. We use the real cap and translate to AED live.
BITOASIS_BTC_MIN_BASE = Decimal("0.000048")


def _to_bitoasis_pair(canonical: str) -> str:
    """Canonical 'BTC/AED' → BitOasis 'BTC-AED'."""
    return canonical.replace("/", "-").upper()


def _dec(value: Any, default: str = "0") -> Decimal:
    """Robust Decimal conversion: handles None, int, float, str."""
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _parse_iso(value: Any) -> datetime:
    """Parse BitOasis ISO 8601 datetime (e.g. '2015-02-12T15:22:22+00:00')."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


class BitOasisAuthError(ExchangeError):
    """Raised on 401/403 — bad token, expired token, or insufficient permissions."""


class BitOasisRateLimitError(ExchangeError):
    """Raised on 429 — too many requests."""


class BitOasisExchange(Exchange):
    name = "bitoasis"

    def __init__(
        self,
        api_token: str,
        dry_run: bool = False,
        timeout_seconds: float = 15.0,
    ):
        if not api_token:
            raise ValueError("BitOasis api_token is required")
        self.dry_run = dry_run
        self._api_token = api_token
        self._client = httpx.AsyncClient(
            base_url=BITOASIS_BASE_URL,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "User-Agent": "bitcoiners-dca/0.2",
                "Accept": "application/json",
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        authenticated: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._api_token}"
        if body is not None:
            headers["Content-Type"] = "application/json"

        try:
            resp = await self._client.request(
                method, path, params=params, json=body, headers=headers
            )
        except httpx.RequestError as e:
            raise ExchangeError(f"BitOasis network error: {e}") from e

        if resp.status_code in (401, 403):
            raise BitOasisAuthError(
                f"BitOasis auth failed ({resp.status_code}): {resp.text[:200]}"
            )
        if resp.status_code == 429:
            raise BitOasisRateLimitError(
                f"BitOasis rate-limited: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ExchangeError(
                f"BitOasis HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            return resp.json()
        except ValueError as e:
            raise ExchangeError(
                f"BitOasis returned non-JSON: {resp.text[:200]}"
            ) from e

    async def _request_with_retry(self, method: str, path: str, **kwargs) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
            retry=retry_if_exception_type(
                (httpx.NetworkError, httpx.TimeoutException, BitOasisRateLimitError)
            ),
            reraise=True,
        ):
            with attempt:
                return await self._request(method, path, **kwargs)

    async def health_check(self) -> bool:
        try:
            await self._request_with_retry("GET", "/exchange/balances")
            logger.info("BitOasis health check OK")
            return True
        except BitOasisAuthError:
            logger.error("BitOasis auth check failed (bad/expired token)")
            raise
        except Exception as e:
            raise ExchangeError(f"BitOasis health check failed: {e}") from e

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
    async def get_ticker(self, pair: str = "BTC/AED") -> Ticker:
        bo = _to_bitoasis_pair(pair)
        data = await self._request(
            "GET", f"/exchange/ticker/{bo}", authenticated=False
        )
        # Response shape: {"ticker": {"pair", "bid", "ask", "last_price", ...}}
        t = data.get("ticker", data)
        return Ticker.from_prices(
            exchange=self.name,
            pair=pair,
            bid=_dec(t.get("bid")),
            ask=_dec(t.get("ask")),
            last=_dec(t.get("last_price") or t.get("last")),
        )

    async def get_fee_schedule(self, pair: str = "BTC/AED") -> FeeSchedule:
        """BitOasis doesn't expose trading fees via the public API.

        We use published retail-tier rates. Override per-account if needed.
        Withdrawal fees ARE exposed at /exchange/coin-withdrawal-fees — we fetch
        the BTC one live, with a safe fallback.
        """
        btc_fee = DEFAULT_BTC_WITHDRAW_FEE
        try:
            data = await self._request(
                "GET", "/exchange/coin-withdrawal-fees", authenticated=False
            )
            withdraw_map = data.get("withdraw", {})
            if "BTC" in withdraw_map:
                btc_fee = _dec(withdraw_map["BTC"], str(DEFAULT_BTC_WITHDRAW_FEE))
        except ExchangeError as e:
            logger.warning("BitOasis withdrawal-fee fetch failed: %s", e)

        return FeeSchedule(
            exchange=self.name,
            pair=pair,
            maker_pct=DEFAULT_MAKER_PCT,
            taker_pct=DEFAULT_TAKER_PCT,
            withdrawal_fee_btc=btc_fee,
        )

    async def get_order_minimum(self, pair: str = "BTC/AED") -> OrderMinimum:
        base, quote = pair.split("/")
        if base == "BTC":
            return OrderMinimum(
                exchange=self.name, pair=pair,
                min_base=BITOASIS_BTC_MIN_BASE,
                min_quote=None,
                quote_currency=quote,
                source="probed",
            )
        return OrderMinimum(
            exchange=self.name, pair=pair,
            quote_currency=quote, source="unknown",
        )

    async def get_balances(self) -> list[Balance]:
        data = await self._request_with_retry("GET", "/exchange/balances")
        # Response shape: {"balances": {"AED": "15.5", "BTC": "0.2756"}}
        # BitOasis returns flat amount strings — no locked/available split exposed.
        raw = data.get("balances", {}) or {}
        out: list[Balance] = []
        for asset, amount in raw.items():
            total = _dec(amount)
            if total > 0:
                out.append(
                    Balance(
                        exchange=self.name,
                        asset=str(asset).upper(),
                        free=total,
                        used=Decimal("0"),
                        total=total,
                    )
                )
        return out

    async def place_market_buy(self, pair: str, quote_amount: Decimal) -> Order:
        """Market buy. BitOasis takes BASE amount, not quote — convert via ticker."""
        if self.dry_run:
            return await self._dry_run_order(pair, quote_amount)

        ticker = await self.get_ticker(pair)
        if ticker.ask <= 0:
            raise ExchangeError(f"BitOasis ticker ask is zero for {pair}")
        base_amount = (quote_amount / ticker.ask).quantize(Decimal("0.00000001"))

        bo = _to_bitoasis_pair(pair)
        body = {
            "pair": bo,
            "side": "buy",
            "type": "market",
            "amount": str(base_amount),
        }
        try:
            data = await self._request_with_retry(
                "POST", "/exchange/order", body=body
            )
        except ExchangeError as e:
            msg = str(e).lower()
            if "insufficient" in msg or "balance" in msg:
                raise InsufficientBalanceError(str(e)) from e
            raise

        order_raw = data.get("order", data)
        order = self._normalize_order(order_raw, pair, quote_amount)
        # Same fill-race as OKX/Binance: BitOasis returns the order in
        # `pending` with filled=0. Poll get_order until settled (or 15s)
        # so multi-hop routes see the real filled amount_base.
        return await self._poll_until_settled(pair, order)

    async def place_limit_buy(
        self,
        pair: str,
        quote_amount: Decimal,
        limit_price: Decimal,
    ) -> Order:
        """BitOasis limit order — body uses BASE amount + limit price (verified
        in docs: POST /exchange/order with type=limit, amount=<base>, price=<limit>).
        """
        if self.dry_run:
            # Dry-run simulates happy path: limit fills at the limit price.
            now = datetime.now(timezone.utc)
            base = (quote_amount / limit_price).quantize(Decimal("0.00000001"))
            return Order(
                exchange=self.name,
                order_id=f"dry-limit-{now.isoformat()}",
                pair=pair, side=OrderSide.BUY, type=OrderType.LIMIT,
                amount_quote=quote_amount, amount_base=base,
                price_filled_avg=limit_price,
                fee_quote=quote_amount * DEFAULT_MAKER_PCT,
                status=OrderStatus.FILLED,
                created_at=now, filled_at=now,
            )

        bo = _to_bitoasis_pair(pair)
        base_amount = (quote_amount / limit_price).quantize(Decimal("0.00000001"))
        body = {
            "pair": bo, "side": "buy", "type": "limit",
            "amount": str(base_amount),
            "price": str(limit_price),
        }
        try:
            data = await self._request_with_retry(
                "POST", "/exchange/order", body=body,
            )
        except ExchangeError as e:
            msg = str(e).lower()
            if "insufficient" in msg or "balance" in msg:
                raise InsufficientBalanceError(str(e)) from e
            raise

        order_raw = data.get("order", data)
        return self._normalize_order(order_raw, pair, quote_amount)

    async def cancel_all_open_orders(self, pair: str) -> int:
        """Cancel every open order on `pair` for this account.

        Override needed because the base implementation calls ccxt's
        `fetch_open_orders` on `self._client` — BitOasis's `_client` is
        an httpx.AsyncClient (no ccxt), so that call raises AttributeError
        which the base catches and returns 0. The pre-cycle sweep would
        therefore silently do nothing on BitOasis, leaving stale
        maker_fallback orders locking up AED across cycles.

        Caveat vs OKX/Binance: BitOasis's API has no clientOrderId
        concept, so we can't filter "bot orders only" — this cancels
        ALL open orders for the pair on the account. Users running the
        bot alongside manual BitOasis orders should be aware. Single-
        purpose accounts (the recommended setup) see no impact.
        """
        bo = _to_bitoasis_pair(pair)
        try:
            data = await self._request(
                "GET",
                f"/exchange/orders/{bo}",
                params={"status": "OPEN"},
            )
        except Exception as e:
            logger.warning("BitOasis fetch open orders failed for %s: %s", pair, e)
            return 0
        orders_raw = data.get("orders", []) or []
        n = 0
        for raw in orders_raw:
            oid = raw.get("id")
            if oid is None:
                continue
            try:
                await self.cancel_order(pair, str(oid))
                n += 1
            except Exception as e:
                logger.warning(
                    "BitOasis cancel_order failed for %s (order %s): %s",
                    pair, oid, e,
                )
                continue
        if n:
            logger.info("BitOasis swept %d open order(s) on %s", n, pair)
        return n

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
        # BitOasis docs: POST /exchange/cancel-order body { "id": <int> }
        try:
            await self._request_with_retry(
                "POST", "/exchange/cancel-order",
                body={"id": int(order_id) if order_id.isdigit() else order_id},
            )
            return await self.get_order(pair, order_id)
        except ExchangeError:
            raise

    async def get_order(self, pair: str, order_id: str) -> Order:
        data = await self._request_with_retry(
            "GET", f"/exchange/order/{order_id}"
        )
        order_raw = data.get("order", data)
        # Reconstruct quote_amount from base × avg fill (or 0 if not filled yet)
        base = _dec(order_raw.get("base_amount"))
        avg = _dec(order_raw.get("avg_execution_price"))
        quote_amount = base * avg if base and avg else Decimal("0")
        return self._normalize_order(order_raw, pair, quote_amount)

    async def get_trade_history(
        self,
        pair: str = "BTC/AED",
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Order]:
        bo = _to_bitoasis_pair(pair)
        params: dict[str, Any] = {"limit": min(limit, 1000), "status": "DONE"}
        if since:
            params["from_date"] = since.date().isoformat()
        data = await self._request_with_retry(
            "GET", f"/exchange/orders/{bo}", params=params
        )
        orders_raw = data.get("orders", []) or []
        result: list[Order] = []
        for raw in orders_raw:
            base = _dec(raw.get("base_amount"))
            avg = _dec(raw.get("avg_execution_price"))
            quote_amount = base * avg if base and avg else Decimal("0")
            result.append(self._normalize_order(raw, pair, quote_amount))
        return result

    async def withdraw_btc(
        self,
        amount_btc: Decimal,
        address: str,
        network: str = "bitcoin",
    ) -> Withdrawal:
        from bitcoiners_dca.core.lightning import is_lightning
        if is_lightning(address) or network.lower() in ("lightning", "ln", "bolt11"):
            raise WithdrawalDeniedError(
                "BitOasis does not support Lightning withdrawals — on-chain only."
            )

        if self.dry_run:
            return Withdrawal(
                exchange=self.name,
                withdrawal_id=f"dry-w-{datetime.now(timezone.utc).isoformat()}",
                asset="BTC",
                amount=amount_btc,
                address=address,
                fee=DEFAULT_BTC_WITHDRAW_FEE,
                status=WithdrawalStatus.PENDING,
                created_at=datetime.now(timezone.utc),
            )

        body = {
            "currency": "BTC",
            "network": network,
            "amount": str(amount_btc),
            "withdrawal_address": address,
        }
        try:
            data = await self._request_with_retry(
                "POST", "/exchange/coin-withdrawal", body=body
            )
        except ExchangeError as e:
            msg = str(e).lower()
            if any(
                signal in msg
                for signal in ("denied", "whitelist", "not allowed", "address")
            ):
                raise WithdrawalDeniedError(str(e)) from e
            raise

        w = data.get("withdrawal", data)
        return Withdrawal(
            exchange=self.name,
            withdrawal_id=str(w.get("id") or ""),
            asset="BTC",
            amount=amount_btc,
            address=address,
            fee=_dec(w.get("amount_fee"), str(DEFAULT_BTC_WITHDRAW_FEE)),
            status=self._map_withdrawal_status(w.get("status")),
            created_at=_parse_iso(w.get("date_created")),
            txid=w.get("tx_hash"),
        )

    async def get_withdrawal(self, withdrawal_id: str) -> Withdrawal:
        data = await self._request_with_retry(
            "GET", f"/exchange/coin-withdrawal/{withdrawal_id}"
        )
        w = data.get("withdrawal", data)
        return Withdrawal(
            exchange=self.name,
            withdrawal_id=str(w.get("id") or withdrawal_id),
            asset=str(w.get("currency", "BTC")).upper(),
            amount=_dec(w.get("value") or w.get("amount")),
            address=w.get("withdrawal_address", ""),
            fee=_dec(w.get("amount_fee"), str(DEFAULT_BTC_WITHDRAW_FEE)),
            status=self._map_withdrawal_status(w.get("status")),
            txid=w.get("tx_hash"),
            created_at=_parse_iso(w.get("date_created")),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator["BitOasisExchange"]:
        try:
            yield self
        finally:
            await self.close()

    async def _dry_run_order(self, pair: str, quote_amount: Decimal) -> Order:
        ticker = await self.get_ticker(pair)
        now = datetime.now(timezone.utc)
        base_amount = (
            quote_amount / ticker.ask if ticker.ask > 0 else Decimal(0)
        )
        return Order(
            exchange=self.name,
            order_id=f"dry-{now.isoformat()}",
            pair=pair,
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            amount_quote=quote_amount,
            amount_base=base_amount,
            price_filled_avg=ticker.ask,
            fee_quote=quote_amount * DEFAULT_TAKER_PCT,
            status=OrderStatus.FILLED,
            created_at=now,
            filled_at=now,
        )

    def _normalize_order(
        self, raw: dict, pair: str, quote_amount: Decimal
    ) -> Order:
        """Translate BitOasis order shape → core Order model.

        BitOasis order JSON (verified):
            {"id", "pair", "side", "type", "base_amount", "price",
             "avg_execution_price", "fee", "date_created", "status"}
        status: OPEN | DONE | CANCELED
        """
        status_str = str(raw.get("status", "OPEN")).upper()
        status = {
            "OPEN": OrderStatus.PENDING,
            "DONE": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "CANCELLED": OrderStatus.CANCELLED,
        }.get(status_str, OrderStatus.PENDING)

        side = OrderSide(str(raw.get("side", "buy")).lower())
        order_type = OrderType(str(raw.get("type", "market")).lower())

        return Order(
            exchange=self.name,
            order_id=str(raw.get("id") or ""),
            pair=pair,
            side=side,
            type=order_type,
            amount_quote=quote_amount,
            amount_base=_dec(raw.get("base_amount")),
            price_filled_avg=_dec(
                raw.get("avg_execution_price") or raw.get("price")
            ),
            fee_quote=_dec(raw.get("fee")),
            status=status,
            created_at=_parse_iso(raw.get("date_created")),
            filled_at=(
                _parse_iso(raw.get("date_created"))
                if status == OrderStatus.FILLED
                else None
            ),
        )

    @staticmethod
    def _map_withdrawal_status(value: Any) -> WithdrawalStatus:
        s = str(value or "PENDING").upper()
        return {
            "PENDING": WithdrawalStatus.PENDING,
            "PROCESSING": WithdrawalStatus.PROCESSING,
            "DONE": WithdrawalStatus.COMPLETE,
            "COMPLETE": WithdrawalStatus.COMPLETE,
            "COMPLETED": WithdrawalStatus.COMPLETE,
            "FAILED": WithdrawalStatus.FAILED,
            "REJECTED": WithdrawalStatus.FAILED,
            "CANCELED": WithdrawalStatus.FAILED,
        }.get(s, WithdrawalStatus.PENDING)
