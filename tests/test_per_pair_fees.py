"""
Per-pair taker fee in router (audit 2026-05-24).

OKX charges substantially higher fees on AED-quoted pairs (~0.6%) than
on USDT-quoted pairs (~0.1%). The router used to fetch ONE taker fee
per exchange and apply it to every hop, which:
  - over-estimated multi-hop route fees (the BTC/USDT leg was priced
    at 0.6% when it's really 0.1%)
  - under-estimated direct AED-route fees IF the lookup pair happened
    to be a USDT pair
  - biased the comparison toward direct AED routes

These tests pin the per-pair lookup behaviour.
"""
from __future__ import annotations

from decimal import Decimal

from bitcoiners_dca.core.router import _ExchangeMarketData


class _FakeExchange:
    name = "okx"


def test_taker_for_returns_per_pair_value_when_present():
    md = _ExchangeMarketData(
        exchange=_FakeExchange(),  # type: ignore[arg-type]
        tickers={},
        taker_pct=Decimal("0.001"),  # default
        balances={},
        taker_pct_by_pair={
            "BTC/AED": Decimal("0.006"),
            "BTC/USDT": Decimal("0.001"),
        },
    )
    assert md.taker_for("BTC/AED") == Decimal("0.006")
    assert md.taker_for("BTC/USDT") == Decimal("0.001")


def test_taker_for_falls_back_to_default_when_pair_missing():
    md = _ExchangeMarketData(
        exchange=_FakeExchange(),  # type: ignore[arg-type]
        tickers={},
        taker_pct=Decimal("0.005"),  # default
        balances={},
        taker_pct_by_pair={"BTC/AED": Decimal("0.006")},
    )
    # Pair not in the override map → falls back to default.
    assert md.taker_for("BTC/USDT") == Decimal("0.005")


def test_empty_per_pair_map_always_uses_default():
    """When no per-pair fees were fetched (e.g. all fee calls failed),
    every lookup returns the default. Preserves prior behaviour."""
    md = _ExchangeMarketData(
        exchange=_FakeExchange(),  # type: ignore[arg-type]
        tickers={},
        taker_pct=Decimal("0.003"),
        balances={},
        # taker_pct_by_pair defaults to {} via field(default_factory=dict)
    )
    assert md.taker_for("BTC/AED") == Decimal("0.003")
    assert md.taker_for("ANY/PAIR") == Decimal("0.003")
