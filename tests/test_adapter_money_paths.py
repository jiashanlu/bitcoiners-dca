"""
Audit 2026-06-10 P1 batch B — adapter money-path fixes.

1. OKX: a blind retry after a network error could DOUBLE-BUY — a market
   order fills instantly, so OKX's duplicate-clOrdId rejection (which only
   guards while the original order is live) doesn't stop a re-placement.
   _place_idempotent now looks the clOrdId up server-side before every
   re-attempt and refuses to re-place when the lookup is inconclusive.

2. BitOasis: `base_amount` is the order SIZE, not the executed quantity —
   reporting it as amount_base made an unfilled/cancelled maker order look
   fully bought (phantom PARTIAL; maker_fallback never fell back).

3. Binance travel-rule: `accepted:false` in a 200 response is a REJECTION,
   not a pending success; and the returned trId lives in the localentity id
   space, so get_withdrawal must poll localentity history too. ccxt
   withdrawal statuses are normalized STRINGS, not raw ints.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import ccxt.async_support as ccxt_async
import pytest

from bitcoiners_dca.core.models import OrderStatus, WithdrawalStatus
from bitcoiners_dca.exchanges.base import ExchangeError, WithdrawalDeniedError
from bitcoiners_dca.exchanges.bitoasis import BitOasisExchange
from bitcoiners_dca.exchanges.binance import BinanceExchange
from bitcoiners_dca.exchanges.okx import OKXExchange


# ─── OKX: lookup-before-retry ──────────────────────────────────────────


def _bare_okx() -> OKXExchange:
    ex = OKXExchange.__new__(OKXExchange)
    ex.name = "okx"
    ex.dry_run = False
    ex._client = AsyncMock()
    return ex


_CCXT_ORDER = {
    "id": "789", "status": "closed", "side": "buy", "type": "market",
    "filled": 0.001, "amount": 0.001, "average": 100000,
    "timestamp": 1750000000000, "fee": None,
}


@pytest.mark.asyncio
async def test_okx_retry_returns_existing_order_instead_of_replacing(monkeypatch):
    """Placement raises a NetworkError but the order actually LANDED and
    filled. The retry must find it by clOrdId and return it — exactly one
    create call ever reaches the exchange."""
    ex = _bare_okx()
    ex._client.create_market_buy_order = AsyncMock(
        side_effect=ccxt_async.NetworkError("conn reset mid-response")
    )
    ex._client.fetch_order = AsyncMock(return_value=_CCXT_ORDER)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    order = await ex._create_market_buy_only(
        "BTC/USDT", Decimal("100"), "bdca-test-1"
    )

    assert order.order_id == "789"
    assert order.status == OrderStatus.FILLED
    assert ex._client.create_market_buy_order.await_count == 1  # no re-place
    ex._client.fetch_order.assert_awaited_with(
        None, "BTC/USDT", params={"clOrdId": "bdca-test-1"}
    )


@pytest.mark.asyncio
async def test_okx_retry_replaces_when_order_confirmed_absent(monkeypatch):
    """Lookup says the failed attempt never landed → re-placing is safe."""
    ex = _bare_okx()
    ex._client.create_market_buy_order = AsyncMock(
        side_effect=[ccxt_async.NetworkError("blip"), _CCXT_ORDER]
    )
    ex._client.fetch_order = AsyncMock(
        side_effect=ccxt_async.OrderNotFound("no such order")
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    order = await ex._create_market_buy_only(
        "BTC/USDT", Decimal("100"), "bdca-test-2"
    )

    assert order.order_id == "789"
    assert ex._client.create_market_buy_order.await_count == 2


@pytest.mark.asyncio
async def test_okx_refuses_to_replace_when_state_unknown(monkeypatch):
    """Placement failed AND the verification lookup failed — the order may
    or may not exist. Re-placing blind risks a double-buy: surface an error
    instead."""
    ex = _bare_okx()
    ex._client.create_market_buy_order = AsyncMock(
        side_effect=ccxt_async.NetworkError("blip")
    )
    ex._client.fetch_order = AsyncMock(
        side_effect=ccxt_async.RequestTimeout("lookup also down")
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with pytest.raises(ExchangeError, match="not re-placing"):
        await ex._create_market_buy_only("BTC/USDT", Decimal("100"), "bdca-test-3")
    assert ex._client.create_market_buy_order.await_count == 1


@pytest.mark.asyncio
async def test_okx_non_retryable_error_surfaces_immediately(monkeypatch):
    ex = _bare_okx()
    ex._client.create_market_buy_order = AsyncMock(
        side_effect=ccxt_async.ExchangeError("51008 insufficient")
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with pytest.raises(ccxt_async.ExchangeError):
        await ex._create_market_buy_only("BTC/USDT", Decimal("100"), "bdca-test-4")
    assert ex._client.create_market_buy_order.await_count == 1


# ─── BitOasis: amount_base must be the EXECUTED quantity ───────────────


def _bitoasis() -> BitOasisExchange:
    return BitOasisExchange(api_token="dummy")


def _bo_raw(status: str, base_amount: str = "0.005", executed=None) -> dict:
    raw = {
        "id": 42, "pair": "BTC-AED", "side": "buy", "type": "limit",
        "base_amount": base_amount, "price": "227000",
        "avg_execution_price": None, "fee": "0",
        "date_created": "2026-06-10T09:00:00+00:00", "status": status,
    }
    if executed is not None:
        raw["executed_amount"] = executed
    return raw


def test_bitoasis_cancelled_unfilled_order_reports_no_fill():
    """The phantom-PARTIAL bug: a CANCELED maker order with zero fills
    reported amount_base = full size, so the strategy recorded a phantom
    partial and maker_fallback never fell back to taker."""
    order = _bitoasis()._normalize_order(
        _bo_raw("CANCELED"), "BTC/AED", Decimal("1000")
    )
    assert order.status == OrderStatus.CANCELLED
    assert order.amount_base is None          # no fill claimed


def test_bitoasis_open_order_reports_no_fill():
    order = _bitoasis()._normalize_order(
        _bo_raw("OPEN"), "BTC/AED", Decimal("1000")
    )
    assert order.status == OrderStatus.PENDING
    assert order.amount_base is None


def test_bitoasis_done_order_reports_base_amount():
    order = _bitoasis()._normalize_order(
        _bo_raw("DONE"), "BTC/AED", Decimal("1000")
    )
    assert order.status == OrderStatus.FILLED
    assert order.amount_base == Decimal("0.005")


def test_bitoasis_executed_amount_wins_when_present():
    """If the API ever reports executed_amount, trust it — including for a
    cancelled order that partially filled before the cancel."""
    order = _bitoasis()._normalize_order(
        _bo_raw("CANCELED", executed="0.002"), "BTC/AED", Decimal("1000")
    )
    assert order.amount_base == Decimal("0.002")


# ─── Binance travel-rule withdrawal ────────────────────────────────────


def _bare_binance() -> BinanceExchange:
    ex = BinanceExchange.__new__(BinanceExchange)
    ex.name = "binance"
    ex.dry_run = False
    ex._client = AsyncMock()
    return ex


@pytest.mark.asyncio
async def test_binance_rejected_travel_rule_withdrawal_raises():
    """accepted:false in a 200 response is a REJECTION — reporting it as a
    PENDING success told the user BTC was on its way when nothing moves."""
    ex = _bare_binance()
    ex._client.sapiPostLocalentityWithdrawApply = AsyncMock(
        return_value={"trId": 555, "accepted": False, "info": "questionnaire invalid"}
    )

    with pytest.raises(WithdrawalDeniedError, match="questionnaire invalid"):
        await ex._withdraw_via_localentity(
            Decimal("0.01"), "bc1qexample", "BTC", None
        )


@pytest.mark.asyncio
async def test_binance_accepted_withdrawal_returns_trid():
    ex = _bare_binance()
    ex._client.sapiPostLocalentityWithdrawApply = AsyncMock(
        return_value={"trId": 555, "accepted": True}
    )
    w = await ex._withdraw_via_localentity(
        Decimal("0.01"), "bc1qexample", "BTC", None
    )
    assert w.withdrawal_id == "555"
    assert w.status == WithdrawalStatus.PENDING


@pytest.mark.asyncio
async def test_binance_get_withdrawal_maps_ccxt_string_statuses():
    """ccxt fetch_withdrawals returns normalized STRING statuses — the old
    int-keyed map never matched, so every withdrawal sat at PENDING."""
    ex = _bare_binance()
    ex._client.fetch_withdrawals = AsyncMock(return_value=[
        {"id": "abc", "status": "ok", "amount": 0.01,
         "address": "bc1q", "txid": "deadbeef", "timestamp": 1750000000000},
    ])
    w = await ex.get_withdrawal("abc")
    assert w.status == WithdrawalStatus.COMPLETE
    assert w.txid == "deadbeef"


@pytest.mark.asyncio
async def test_binance_get_withdrawal_finds_travel_rule_trid():
    """A localentity trId never shows up in the capital history — the
    fallback must poll localentity history and map its raw int statuses."""
    ex = _bare_binance()
    ex._client.fetch_withdrawals = AsyncMock(return_value=[])
    ex._client.sapiGetLocalentityWithdrawHistory = AsyncMock(return_value=[
        {"trId": 555, "coin": "BTC", "amount": "0.01", "status": 6,
         "address": "bc1q", "txId": "cafebabe", "transactionFee": "0.0002"},
    ])
    w = await ex.get_withdrawal("555")
    assert w.status == WithdrawalStatus.COMPLETE
    assert w.txid == "cafebabe"


@pytest.mark.asyncio
async def test_binance_get_withdrawal_travel_rule_rejection_maps_failed():
    ex = _bare_binance()
    ex._client.fetch_withdrawals = AsyncMock(return_value=[])
    ex._client.sapiGetLocalentityWithdrawHistory = AsyncMock(return_value=[
        {"trId": 556, "coin": "BTC", "amount": "0.01", "status": 3,
         "address": "bc1q", "txId": None},
    ])
    w = await ex.get_withdrawal("556")
    assert w.status == WithdrawalStatus.FAILED


# ─── audit 2026-06-10 P2/P3: sweep failure surfacing + reraise ─────────


@pytest.mark.asyncio
async def test_sweep_raises_when_bot_order_cannot_be_cancelled():
    """A KNOWN stale bot order that fails to cancel can fill on top of the
    new cycle's buy — the sweep must surface that, not swallow it."""
    from bitcoiners_dca.exchanges.base import BOT_CLORD_PREFIX

    ex = _bare_okx()
    ex._client.fetch_open_orders = AsyncMock(return_value=[
        {"id": "stale-1", "clientOrderId": f"{BOT_CLORD_PREFIX}abc"},
    ])
    ex.cancel_order = AsyncMock(side_effect=RuntimeError("cancel rejected"))

    with pytest.raises(ExchangeError, match="FAILED to cancel"):
        await ex.cancel_all_open_orders("BTC/USDT")


@pytest.mark.asyncio
async def test_sweep_list_failure_returns_zero_not_raise():
    """Pairs the venue doesn't list fail the LIST step every cycle — that
    stays non-fatal (warning), unlike a failed cancel of a known order."""
    ex = _bare_okx()
    ex._client.fetch_open_orders = AsyncMock(side_effect=RuntimeError("no such pair"))
    assert await ex.cancel_all_open_orders("BTC/USDC") == 0


@pytest.mark.asyncio
async def test_health_check_reraises_real_error_not_retryerror():
    """tenacity must surface the REAL final exception (reraise=True) so
    error classification and user messages see 'okx 50110: ...', not
    'RetryError[<Future ...>]'."""
    ex = _bare_okx()
    ex._client.load_markets = AsyncMock(
        side_effect=ccxt_async.AuthenticationError("okx 50110: IP not whitelisted")
    )
    ex._client.fetch_balance = AsyncMock()

    # The adapter wraps into ExchangeError carrying the REAL message —
    # reraise=True means that's what callers get, never RetryError.
    with pytest.raises(ExchangeError, match="50110"):
        await ex.health_check()
