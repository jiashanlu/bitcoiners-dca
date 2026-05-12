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
