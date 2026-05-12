"""
Tests for MarketDataProvider — uses a fake HistoricalPriceSource so we don't
hit CoinGecko in unit tests.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bitcoiners_dca.core.historical_prices import PricePoint
from bitcoiners_dca.core.market_data import MarketDataProvider
from bitcoiners_dca.persistence.db import Database


class _FakeHistorySource:
    """Returns whatever points are passed in. No network."""
    def __init__(self, points): self._points = points
    def fetch(self, vs_currency="aed", days=365): return self._points


def _flat_series(start: datetime, days: int, price: str = "300000") -> list[PricePoint]:
    return [
        PricePoint(
            timestamp=(start - timedelta(days=days - i)).replace(tzinfo=timezone.utc),
            price=Decimal(price),
        )
        for i in range(days)
    ]


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    yield db
    db.close()


def test_snapshot_extracts_seven_and_thirty_day_history(db):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    points = [
        PricePoint(timestamp=(now - timedelta(days=d)), price=Decimal(str(300000 + d * 100)))
        for d in range(40)
    ]
    provider = MarketDataProvider(db=db, history_source=_FakeHistorySource(points))
    snap = provider.snapshot(now=now)
    assert snap.price_7d_ago_aed == Decimal("300700")
    assert snap.price_30d_ago_aed == Decimal("303000")


def test_snapshot_finds_ath_over_window(db):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    points = [
        PricePoint(timestamp=(now - timedelta(days=d)), price=Decimal(str(300000)))
        for d in range(365)
    ]
    # Spike at day 100
    points[100] = PricePoint(
        timestamp=(now - timedelta(days=100)), price=Decimal("450000"),
    )
    provider = MarketDataProvider(db=db, history_source=_FakeHistorySource(points))
    snap = provider.snapshot(now=now)
    assert snap.price_ath_aed == Decimal("450000")


def test_realized_vol_zero_on_flat_series(db):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    flat = _flat_series(now, 30, price="300000")
    provider = MarketDataProvider(db=db, history_source=_FakeHistorySource(flat))
    snap = provider.snapshot(now=now)
    # Flat prices → zero realized vol
    assert snap.realized_vol_30d_pct == Decimal("0.00")


def test_realized_vol_nonzero_on_volatile_series(db):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    # Alternate +5%, -5% daily → high realized vol
    points = []
    price = 300000.0
    for d in range(30):
        points.append(PricePoint(
            timestamp=(now - timedelta(days=29 - d)),
            price=Decimal(str(round(price, 2))),
        ))
        price *= 1.05 if d % 2 == 0 else 0.95
    provider = MarketDataProvider(db=db, history_source=_FakeHistorySource(points))
    snap = provider.snapshot(now=now)
    # Should land in a high-vol bucket — 80%+ annualized
    assert snap.realized_vol_30d_pct > Decimal("80")


def test_snapshot_handles_empty_history_gracefully(db):
    provider = MarketDataProvider(db=db, history_source=_FakeHistorySource([]))
    snap = provider.snapshot()
    assert snap.price_7d_ago_aed is None
    assert snap.price_ath_aed is None
    assert snap.realized_vol_30d_pct is None


def test_snapshot_caches_within_window(db):
    """Second call returns the cached object, doesn't refetch."""
    call_count = [0]

    class CountingSource:
        def fetch(self, vs_currency="aed", days=365):
            call_count[0] += 1
            return []

    provider = MarketDataProvider(db=db, history_source=CountingSource())
    provider.snapshot()
    provider.snapshot()
    assert call_count[0] == 1


def test_context_dict_roundtrip(db):
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    points = _flat_series(now, 30, price="300000")
    provider = MarketDataProvider(db=db, history_source=_FakeHistorySource(points))
    ctx = provider.snapshot(now=now).to_context_dict()
    assert ctx["price_ath_aed"] == Decimal("300000")
    assert "realized_vol_30d_pct" in ctx
