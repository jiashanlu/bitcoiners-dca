# Scheduler unit tests. Focused on the cron-trigger builder since it
# carries the only branching logic in the scheduler. Hourly was added
# 2026-05-12; the rest of these test the pre-existing branches too so a
# future scheduler refactor can rely on this for regression coverage.
from bitcoiners_dca.core.scheduler import _build_cron_trigger
from bitcoiners_dca.utils.config import AppConfig, StrategyYamlConfig


def _cfg(frequency: str, time: str = "09:15", day_of_week: str = "monday") -> AppConfig:
    cfg = AppConfig()
    cfg.strategy = StrategyYamlConfig(
        amount_aed=100,
        frequency=frequency,
        day_of_week=day_of_week,
        time=time,
        timezone="UTC",
    )
    return cfg


def _trigger_fields(trig) -> dict[str, str]:
    return {f.name: str(f) for f in trig.fields}


def test_hourly_fires_every_hour_at_configured_minute():
    trig = _build_cron_trigger(_cfg("hourly", time="00:15"))
    f = _trigger_fields(trig)
    assert f["minute"] == "15"
    assert f["hour"] == "*"


def test_daily_pins_hour_and_minute():
    trig = _build_cron_trigger(_cfg("daily", time="09:30"))
    f = _trigger_fields(trig)
    assert f["hour"] == "9"
    assert f["minute"] == "30"


def test_weekly_pins_day_of_week():
    trig = _build_cron_trigger(_cfg("weekly", day_of_week="tuesday", time="08:00"))
    f = _trigger_fields(trig)
    assert f["day_of_week"] == "tue"
    assert f["hour"] == "8"
    assert f["minute"] == "0"


def test_monthly_pins_day_one():
    trig = _build_cron_trigger(_cfg("monthly", time="07:00"))
    f = _trigger_fields(trig)
    assert f["day"] == "1"
    assert f["hour"] == "7"


def test_invalid_frequency_raises():
    import pytest
    with pytest.raises(ValueError, match="Invalid frequency"):
        _build_cron_trigger(_cfg("yearly"))
