"""
FundingMonitor tests — threshold + cooldown logic with a mocked HTTP layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.funding_monitor import FundingMonitor, FundingReading
from bitcoiners_dca.persistence.db import Database


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    yield db
    db.close()


def _reading(ann_pct: str, exchange="okx", instrument="BTC-USDT-SWAP"):
    return FundingReading(
        instrument=instrument, exchange=exchange,
        rate_per_period=Decimal("0.0001"),
        annualized_pct=Decimal(ann_pct),
        settles_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )


def test_alert_fires_above_positive_threshold(db):
    m = FundingMonitor(db=db, alert_threshold_pct=Decimal("15"))
    msg = m.evaluate_alert(_reading("20"))
    assert msg is not None
    assert "+20.00%" in msg
    assert "longs paying shorts" in msg


def test_alert_fires_below_negative_threshold(db):
    m = FundingMonitor(
        db=db,
        alert_threshold_pct=Decimal("15"),
        alert_negative_threshold_pct=Decimal("-10"),
    )
    msg = m.evaluate_alert(_reading("-15"))
    assert msg is not None
    assert "shorts paying longs" in msg


def test_no_alert_when_in_band(db):
    m = FundingMonitor(db=db, alert_threshold_pct=Decimal("15"))
    assert m.evaluate_alert(_reading("5")) is None
    assert m.evaluate_alert(_reading("-5")) is None


def test_cooldown_suppresses_second_alert(db):
    m = FundingMonitor(
        db=db, alert_threshold_pct=Decimal("15"),
        alert_cooldown_hours=24,
    )
    first = m.evaluate_alert(_reading("20"))
    second = m.evaluate_alert(_reading("22"))
    assert first is not None
    assert second is None  # cooldown


def test_separate_instruments_have_independent_cooldowns(db):
    m = FundingMonitor(db=db, alert_threshold_pct=Decimal("15"))
    first = m.evaluate_alert(_reading("20", instrument="BTC-USDT-SWAP"))
    second = m.evaluate_alert(_reading("20", instrument="ETH-USDT-SWAP"))
    assert first is not None
    assert second is not None  # different instrument → independent cooldown


# ─── Cadence derivation ────────────────────────────────────────────────
#
# Regression: 8h, 4h, and 1h funding intervals all annualize correctly.
# The old code hardcoded 1095 settlements/year (8h-only); now we derive
# from OKX's fundingTime/nextFundingTime fields. These tests exercise
# `_fetch_okx` against a stub httpx client so we never touch the network.


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _StubClient:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, headers=None):
        return _StubResponse(self._payload)


def _okx_payload(rate: str, ft_ms: int, nft_ms: int) -> dict:
    return {
        "code": "0",
        "data": [{
            "fundingRate": rate,
            "fundingTime": str(ft_ms),
            "nextFundingTime": str(nft_ms),
        }],
    }


@pytest.mark.asyncio
async def test_okx_cadence_8h_annualizes_at_1095_periods(db):
    """A 0.01% per 8h rate should annualize to 0.01 × 1095 = ~10.95%."""
    monitor = FundingMonitor(db=db)
    ft = 1_700_000_000_000
    nft = ft + 8 * 60 * 60 * 1000  # +8h
    client = _StubClient(_okx_payload("0.0001", ft, nft))
    reading = await monitor._fetch_okx(client, "BTC-USDT-SWAP")
    # 0.0001 × (365 * 24 / 8) × 100 = 0.0001 × 1095 × 100 = 10.95
    assert abs(reading.annualized_pct - Decimal("10.95")) < Decimal("0.01")


@pytest.mark.asyncio
async def test_okx_cadence_4h_annualizes_at_2190_periods(db):
    """A 0.01% per 4h rate should annualize ~2× the 8h version (21.9%)."""
    monitor = FundingMonitor(db=db)
    ft = 1_700_000_000_000
    nft = ft + 4 * 60 * 60 * 1000  # +4h
    client = _StubClient(_okx_payload("0.0001", ft, nft))
    reading = await monitor._fetch_okx(client, "BTC-USDT-SWAP")
    # 0.0001 × 2190 × 100 = 21.9
    assert abs(reading.annualized_pct - Decimal("21.9")) < Decimal("0.05")


@pytest.mark.asyncio
async def test_okx_cadence_falls_back_to_8h_when_times_missing(db):
    """If OKX response is missing fundingTime/nextFundingTime, fall back
    to the historical 8h default (1095 periods)."""
    monitor = FundingMonitor(db=db)
    nft = 1_700_000_000_000 + 8 * 60 * 60 * 1000
    payload = {
        "code": "0",
        "data": [{
            "fundingRate": "0.0001",
            # `fundingTime` deliberately absent
            "nextFundingTime": str(nft),
        }],
    }
    client = _StubClient(payload)
    reading = await monitor._fetch_okx(client, "BTC-USDT-SWAP")
    # Falls back to 1095, same as the 8h case.
    assert abs(reading.annualized_pct - Decimal("10.95")) < Decimal("0.01")
