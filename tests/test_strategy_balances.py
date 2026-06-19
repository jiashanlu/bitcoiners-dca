"""
Post-cycle balance reminder — aggregation, best-effort snapshot, and the
Telegram render.

Ben asked (2026-06-16) for the per-cycle trade notification to carry an
AED/USD dry-powder + BTC stack reminder. The snapshot is built after the
trade is recorded and must NEVER affect execution: a failing exchange
degrades to a partial snapshot, never an exception.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.models import (
    Balance,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from bitcoiners_dca.core.notifications import Notifier, _format_balances
from bitcoiners_dca.core.strategy import (
    BalanceSnapshot,
    DCAStrategy,
    ExecutionResult,
    aggregate_balances,
)
from bitcoiners_dca.utils.config import NotificationsConfig


def _bal(asset: str, total: str, *, exchange: str = "okx") -> Balance:
    t = Decimal(total)
    return Balance(exchange=exchange, asset=asset, free=t, used=Decimal(0), total=t)


# ─── aggregate_balances ────────────────────────────────────────────────


def test_buckets_aed_usd_stables_and_btc():
    snap = aggregate_balances({
        "okx": [
            _bal("AED", "1234.50"),
            _bal("USDT", "100"),
            _bal("USDC", "50"),
            _bal("BTC", "0.05"),
            _bal("DOGE", "999"),  # ignored — not a tracked bucket
        ]
    })
    assert snap.aed == Decimal("1234.50")
    assert snap.usd_stable == Decimal("150")   # USDT + USDC summed
    assert snap.btc == Decimal("0.05")


def test_sums_across_multiple_exchanges_with_breakdown():
    snap = aggregate_balances({
        "okx": [_bal("AED", "1000", exchange="okx"), _bal("BTC", "0.10", exchange="okx")],
        "binance": [_bal("USDT", "200", exchange="binance"), _bal("BTC", "0.02", exchange="binance")],
    })
    assert snap.aed == Decimal("1000")
    assert snap.usd_stable == Decimal("200")
    assert snap.btc == Decimal("0.12")
    assert snap.per_exchange["okx"]["BTC"] == Decimal("0.10")
    assert snap.per_exchange["binance"]["USD"] == Decimal("200")
    assert snap.has_data is True


def test_asset_matching_is_case_insensitive():
    snap = aggregate_balances({"okx": [_bal("btc", "1"), _bal("usdt", "5")]})
    assert snap.btc == Decimal("1")
    assert snap.usd_stable == Decimal("5")


def test_empty_input_has_no_data():
    snap = aggregate_balances({})
    assert snap.has_data is False
    assert snap.aed == snap.usd_stable == snap.btc == Decimal(0)


# ─── _snapshot_balances (best-effort, never raises) ────────────────────


class _FakeExchange:
    def __init__(self, name: str, balances=None, raises: Exception | None = None):
        self.name = name
        self._balances = balances or []
        self._raises = raises

    async def get_balances(self):
        if self._raises is not None:
            raise self._raises
        return self._balances


def _strategy() -> DCAStrategy:
    # router is unused by _snapshot_balances; pass a placeholder.
    return DCAStrategy.__new__(DCAStrategy)


@pytest.mark.asyncio
async def test_snapshot_aggregates_live_exchanges():
    strat = _strategy()
    exchanges = [
        _FakeExchange("okx", [_bal("AED", "500"), _bal("BTC", "0.03")]),
        _FakeExchange("binance", [_bal("USDT", "75")]),
    ]
    snap = await strat._snapshot_balances(exchanges)
    assert snap.aed == Decimal("500")
    assert snap.usd_stable == Decimal("75")
    assert snap.btc == Decimal("0.03")
    assert snap.errors == []


@pytest.mark.asyncio
async def test_snapshot_degrades_when_one_exchange_fails():
    """A venue whose balance call throws is named in errors and omitted from
    the totals — the reminder stays partial-but-honest, never crashes."""
    strat = _strategy()
    exchanges = [
        _FakeExchange("okx", [_bal("AED", "500")]),
        _FakeExchange("binance", raises=RuntimeError("401 auth")),
    ]
    snap = await strat._snapshot_balances(exchanges)
    assert snap.aed == Decimal("500")
    assert snap.errors == ["binance"]
    assert "binance" not in snap.per_exchange


@pytest.mark.asyncio
async def test_snapshot_all_failing_yields_no_data():
    strat = _strategy()
    exchanges = [_FakeExchange("okx", raises=RuntimeError("boom"))]
    snap = await strat._snapshot_balances(exchanges)
    assert snap.has_data is False
    assert snap.errors == ["okx"]


# ─── render in the cycle message ───────────────────────────────────────


def _executed_result(balances: BalanceSnapshot | None) -> ExecutionResult:
    order = Order(
        exchange="okx", order_id="x", pair="BTC/AED", side=OrderSide.BUY,
        type=OrderType.MARKET, amount_quote=Decimal("50"),
        amount_base=Decimal("0.00017"), price_filled_avg=Decimal("290000"),
        fee_base=Decimal(0), fee_quote=Decimal(0), status=OrderStatus.FILLED,
        created_at=datetime.now(timezone.utc),
    )
    return ExecutionResult(
        timestamp=datetime.now(timezone.utc),
        intended_amount_aed=Decimal("50"),
        overlay_applied=None,
        routing_decision=None,
        orders=[order],
        balances=balances,
    )


def test_cycle_message_includes_balance_reminder():
    snap = aggregate_balances({"okx": [_bal("AED", "812.34"), _bal("USDT", "120"), _bal("BTC", "0.057")]})
    msg = Notifier(NotificationsConfig())._format_cycle_message(_executed_result(snap))
    assert "💰 *Balance reminder*" in msg
    assert "*AED:* 812.34" in msg
    assert "*USD (USDT/USDC):* 120" in msg
    assert "*BTC:* 0.057" in msg


def test_cycle_message_omits_reminder_when_no_snapshot():
    msg = Notifier(NotificationsConfig())._format_cycle_message(_executed_result(None))
    assert "Balance reminder" not in msg


def test_cycle_message_omits_reminder_when_disabled_in_config():
    snap = aggregate_balances({"okx": [_bal("AED", "812.34")]})
    cfg = NotificationsConfig(include_balance_reminder=False)
    msg = Notifier(cfg)._format_cycle_message(_executed_result(snap))
    assert "Balance reminder" not in msg


def test_render_shows_per_exchange_breakdown_when_multi_venue():
    snap = aggregate_balances({
        "okx": [_bal("AED", "500", exchange="okx"), _bal("BTC", "0.10", exchange="okx")],
        "binance": [_bal("USDT", "200", exchange="binance")],
    })
    out = _format_balances(snap)
    assert "okx:" in out
    assert "binance:" in out


def test_render_flags_partial_snapshot():
    snap = aggregate_balances({"okx": [_bal("AED", "500")]})
    snap.errors = ["binance"]
    out = _format_balances(snap)
    assert "partial" in out
    assert "binance" in out
