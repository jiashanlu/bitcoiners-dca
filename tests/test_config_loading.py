"""
Config loading after the licence/tier system was removed (2026-10-07).

Every feature is available to everyone, so:
  - a config.yaml written by an older release, still carrying a
    `license:` section (tier + signed key), must keep loading;
  - features that used to be tier-gated (multi-exchange, multi-hop
    routing, maker mode, overlays, funding monitor) reach the runtime
    exactly as the user configured them.
"""
from __future__ import annotations

import logging
from textwrap import dedent

import pytest

from bitcoiners_dca.cli import _build_overlays, _build_router
from bitcoiners_dca.utils import config as config_module
from bitcoiners_dca.utils.config import load_config

LEGACY_LICENSED_CONFIG = dedent("""
    license:
      tier: pro
      key: legacy-signed-token-from-before-2026-10
    strategy:
      amount_aed: 500
      frequency: weekly
    exchanges:
      okx:
        enabled: true
      binance:
        enabled: true
      bitoasis:
        enabled: true
""")

FULL_FEATURE_CONFIG = dedent("""
    exchanges:
      okx:
        enabled: true
      binance:
        enabled: true
      bitoasis:
        enabled: true
    routing:
      enable_two_hop: true
      enable_cross_exchange_alerts: true
    execution:
      mode: maker_fallback
    funding_monitor:
      enabled: true
    overlays:
      buy_the_dip:
        enabled: true
      volatility_weighted:
        enabled: true
      time_of_day:
        enabled: true
      drawdown_aware:
        enabled: true
      onchain_smart_trigger:
        enabled: true
""")


@pytest.fixture
def write_config(tmp_path):
    def _write(text: str):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return path
    return _write


@pytest.fixture(autouse=True)
def _reset_retired_section_warnings(monkeypatch):
    monkeypatch.setattr(config_module, "_retired_sections_warned", set())


def test_legacy_license_section_still_loads(write_config):
    cfg = load_config(write_config(LEGACY_LICENSED_CONFIG))

    assert cfg.strategy.amount_aed == 500
    assert not hasattr(cfg, "license")


def test_legacy_license_section_logs_one_deprecation_line(write_config, caplog):
    path = write_config(LEGACY_LICENSED_CONFIG)

    with caplog.at_level(logging.WARNING, logger=config_module.__name__):
        load_config(path)
        load_config(path)  # daemon hot-reloads must not re-log it

    deprecations = [r for r in caplog.records if "`license:`" in r.getMessage()]
    assert len(deprecations) == 1


def test_legacy_license_key_is_never_logged(write_config, caplog):
    with caplog.at_level(logging.DEBUG):
        load_config(write_config(LEGACY_LICENSED_CONFIG))

    assert "legacy-signed-token" not in caplog.text


def test_all_exchanges_stay_enabled(write_config):
    cfg = load_config(write_config(FULL_FEATURE_CONFIG))

    assert cfg.exchanges.okx.enabled
    assert cfg.exchanges.binance.enabled
    assert cfg.exchanges.bitoasis.enabled


def test_routing_and_execution_features_stay_enabled(write_config):
    cfg = load_config(write_config(FULL_FEATURE_CONFIG))
    router = _build_router(cfg)

    assert router.enable_two_hop is True
    assert router.enable_cross_exchange_alerts is True
    assert cfg.execution.mode == "maker_fallback"
    assert cfg.funding_monitor.enabled is True


def test_every_configured_overlay_is_built(write_config):
    cfg = load_config(write_config(FULL_FEATURE_CONFIG))

    overlay_types = {type(o).__name__ for o in _build_overlays(cfg)}

    assert overlay_types == {
        "BuyTheDipOverlay",
        "VolatilityWeightedOverlay",
        "TimeOfDayOverlay",
        "DrawdownOverlay",
        "OnchainSmartTriggerOverlay",
    }
