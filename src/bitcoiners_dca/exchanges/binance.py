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
                exchange=self.name, order_id=f"dry-{datetime.utcnow().isoformat()}",
                pair=pair, side=OrderSide.BUY, type=OrderType.MARKET,
                amount_quote=quote_amount, amount_base=quote_amount / ticker.ask,
                price_filled_avg=ticker.ask, fee_quote=quote_amount * Decimal("0.001"),
                status=OrderStatus.FILLED, created_at=datetime.utcnow(), filled_at=datetime.utcnow(),
            )
        # Binance uses quoteOrderQty for AED-amount market buys
        try:
            raw = await self._client.create_market_buy_order(
                symbol=pair, amount=float(quote_amount),
                params={
                    "quoteOrderQty": float(quote_amount),
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
            now = datetime.utcnow()
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
            now = datetime.utcnow()
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
                           network: str = "bitcoin") -> Withdrawal:
        from bitcoiners_dca.core.lightning import is_lightning
        if is_lightning(address) or network.lower() in ("lightning", "ln", "bolt11"):
            raise WithdrawalDeniedError(
                "Binance UAE does not support Lightning withdrawals — on-chain only. "
                "Use OKX for Lightning withdrawals."
            )

        if self.dry_run:
            return Withdrawal(
                exchange=self.name, withdrawal_id=f"dry-w-{datetime.utcnow().isoformat()}",
                asset="BTC", amount=amount_btc, address=address, fee=Decimal("0.0002"),
                status=WithdrawalStatus.PENDING, created_at=datetime.utcnow(),
            )
        try:
            params = {"network": "BTC" if network == "bitcoin" else network}
            raw = await self._client.withdraw(
                code="BTC", amount=float(amount_btc), address=address, params=params,
            )
            return Withdrawal(
                exchange=self.name, withdrawal_id=str(raw.get("id") or ""),
                asset="BTC", amount=amount_btc, address=address,
                fee=Decimal("0.0002"),
                status=WithdrawalStatus.PENDING, created_at=datetime.utcnow(),
                txid=raw.get("txid"),
            )
        except ccxt_async.PermissionDenied as e:
            raise WithdrawalDeniedError(str(e)) from e
        except Exception as e:
            raise ExchangeError(f"Binance withdraw failed: {e}") from e

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
                    created_at=datetime.fromtimestamp(w.get("timestamp", 0) / 1000),
                )
        raise ExchangeError(f"Binance withdrawal {withdrawal_id} not found")

    def _normalize_order(self, raw: dict, pair: str, quote_amount: Decimal) -> Order:
        status_map = {
            "open": OrderStatus.PENDING, "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED, "cancelled": OrderStatus.CANCELLED,
        }
        return Order(
            exchange=self.name, order_id=str(raw.get("id") or ""), pair=pair,
            side=OrderSide(raw.get("side", "buy")), type=OrderType(raw.get("type", "market")),
            amount_quote=quote_amount, amount_base=_to_decimal(raw.get("filled")),
            price_filled_avg=_to_decimal(raw.get("average") or raw.get("price")),
            fee_quote=_to_decimal(raw.get("fee", {}).get("cost") if isinstance(raw.get("fee"), dict) else 0),
            status=status_map.get(raw.get("status", "open"), OrderStatus.PENDING),
            created_at=datetime.fromtimestamp(raw.get("timestamp", 0) / 1000) if raw.get("timestamp") else datetime.utcnow(),
            filled_at=datetime.utcnow() if raw.get("status") == "closed" else None,
        )

    def _normalize_trade_as_order(self, trade: dict, pair: str) -> Order:
        return Order(
            exchange=self.name, order_id=str(trade.get("order") or trade.get("id") or ""),
            pair=pair, side=OrderSide(trade.get("side", "buy")),
            type=OrderType.MARKET,
            amount_quote=_to_decimal(trade.get("cost")), amount_base=_to_decimal(trade.get("amount")),
            price_filled_avg=_to_decimal(trade.get("price")),
            fee_quote=_to_decimal(trade.get("fee", {}).get("cost") if isinstance(trade.get("fee"), dict) else 0),
            status=OrderStatus.FILLED,
            created_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000),
            filled_at=datetime.fromtimestamp(trade.get("timestamp", 0) / 1000),
        )

    async def close(self) -> None:
        await self._client.close()
