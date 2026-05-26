"""
OKX AED-pair fee override (audit 2026-05-26).

ccxt's `load_markets` returns the user's standard spot-tier fees
(typically 0.08% maker / 0.10% taker) for every market — including
OKX's AED-quoted fiat market. But OKX's AED market has its own (much
higher) fee schedule: ~0.40% maker, ~0.60% taker.

Without this override the smart router under-prices AED-leg routes vs
stablecoin-leg routes — a 6× error that biased every benbois cycle
toward direct BTC/AED.

`get_fee_schedule` MUST floor AED-quoted pairs at 0.40/0.60. Stable-
quoted pairs MUST pass through whatever ccxt returned.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from bitcoiners_dca.exchanges.okx import OKXExchange


def _make_okx_with_mock_client(markets: dict, currencies: dict | None = None) -> OKXExchange:
    """Build an OKXExchange with ccxt calls stubbed out."""
    ex = OKXExchange.__new__(OKXExchange)  # skip __init__ (no real creds)
    ex.name = "okx"
    ex._client = AsyncMock()
    ex._client.load_markets = AsyncMock(return_value=markets)
    ex._client.fetch_currencies = AsyncMock(
        return_value=currencies or {
            "BTC": {"networks": {"Bitcoin": {"fee": 0.0002}}},
        }
    )
    return ex


@pytest.mark.asyncio
async def test_btc_aed_floors_to_aed_tier_when_ccxt_returns_spot_tier():
    """The bug: ccxt reports the user's L1 spot tier (0.10/0.15) for
    BTC/AED. Override floors that to OKX's published AED rates."""
    ex = _make_okx_with_mock_client({
        "BTC/AED": {"maker": 0.001, "taker": 0.0015},
    })
    fs = await ex.get_fee_schedule("BTC/AED")
    assert fs.maker_pct == Decimal("0.0040")
    assert fs.taker_pct == Decimal("0.0060")


@pytest.mark.asyncio
async def test_usdt_aed_also_floors():
    """Every AED-quoted pair gets the same treatment — fiat market is
    market-wide, not pair-specific."""
    ex = _make_okx_with_mock_client({
        "USDT/AED": {"maker": 0.001, "taker": 0.0015},
    })
    fs = await ex.get_fee_schedule("USDT/AED")
    assert fs.maker_pct == Decimal("0.0040")
    assert fs.taker_pct == Decimal("0.0060")


@pytest.mark.asyncio
async def test_usdc_aed_also_floors():
    ex = _make_okx_with_mock_client({
        "USDC/AED": {"maker": 0.001, "taker": 0.0015},
    })
    fs = await ex.get_fee_schedule("USDC/AED")
    assert fs.maker_pct == Decimal("0.0040")
    assert fs.taker_pct == Decimal("0.0060")


@pytest.mark.asyncio
async def test_btc_usdt_passes_through_unchanged():
    """Stable-quoted pairs are correctly priced by ccxt — must not get
    floored to the AED rate."""
    ex = _make_okx_with_mock_client({
        "BTC/USDT": {"maker": 0.0008, "taker": 0.001},
    })
    fs = await ex.get_fee_schedule("BTC/USDT")
    assert fs.maker_pct == Decimal("0.0008")
    assert fs.taker_pct == Decimal("0.001")


@pytest.mark.asyncio
async def test_usdc_usdt_passes_through_unchanged():
    ex = _make_okx_with_mock_client({
        "USDC/USDT": {"maker": 0.0002, "taker": 0.0005},
    })
    fs = await ex.get_fee_schedule("USDC/USDT")
    assert fs.maker_pct == Decimal("0.0002")
    assert fs.taker_pct == Decimal("0.0005")


@pytest.mark.asyncio
async def test_floor_uses_max_so_higher_ccxt_values_win():
    """If OKX ever surfaces the real AED-tier fees through ccxt and
    they exceed the floor (e.g. higher account tier punished), trust
    ccxt — don't clamp DOWN to our hardcoded floor."""
    ex = _make_okx_with_mock_client({
        "BTC/AED": {"maker": 0.007, "taker": 0.010},  # higher than floor
    })
    fs = await ex.get_fee_schedule("BTC/AED")
    assert fs.maker_pct == Decimal("0.007")
    assert fs.taker_pct == Decimal("0.010")


@pytest.mark.asyncio
async def test_missing_market_defaults_then_floors():
    """Pair not in markets dict — ccxt's default kicks in (0.001/0.0015),
    then the AED floor lifts it to 0.40/0.60."""
    ex = _make_okx_with_mock_client(markets={})  # no entries at all
    fs = await ex.get_fee_schedule("BTC/AED")
    assert fs.maker_pct == Decimal("0.0040")
    assert fs.taker_pct == Decimal("0.0060")
