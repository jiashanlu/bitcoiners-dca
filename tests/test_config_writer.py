"""
ConfigWriter tests — patch + validate + atomic write.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from bitcoiners_dca.web.config_writer import (
    ConfigPatchResult,
    ConfigWriter,
    ConfigWriteError,
    _get_dotted,
    _set_dotted,
)


@pytest.fixture
def cfg_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "strategy": {"amount_aed": "500", "frequency": "weekly"},
        "execution": {"mode": "taker"},
        "exchanges": {
            "okx": {"enabled": False},
            "bitoasis": {"enabled": True, "token_env": "BITOASIS_API_TOKEN"},
        },
        "dry_run": True,
    }))
    return path


# === dotted-path helpers ===

def test_get_dotted_walks_nested():
    d = {"a": {"b": {"c": 42}}}
    assert _get_dotted(d, "a.b.c") == 42
    assert _get_dotted(d, "a.b") == {"c": 42}
    assert _get_dotted(d, "a.x") is None
    assert _get_dotted(d, "x") is None


def test_set_dotted_creates_intermediates():
    d: dict = {}
    _set_dotted(d, "a.b.c", 42)
    assert d == {"a": {"b": {"c": 42}}}


def test_set_dotted_overwrites_existing():
    d = {"a": {"b": 1}}
    _set_dotted(d, "a.b", 2)
    assert d == {"a": {"b": 2}}


# === patch + save ===

def test_patch_applies_changes_and_writes(cfg_file):
    w = ConfigWriter(cfg_file)
    result = w.patch_and_save({
        "strategy.amount_aed": "750",
        "execution.mode": "maker_fallback",
    })

    assert sorted(result.changed_keys) == ["execution.mode", "strategy.amount_aed"]

    # Re-read from disk to confirm persistence
    raw = yaml.safe_load(cfg_file.read_text())
    assert raw["strategy"]["amount_aed"] == "750"
    assert raw["execution"]["mode"] == "maker_fallback"

    # New AppConfig is returned + validates
    assert result.new_config.strategy.amount_aed == Decimal("750")
    assert result.new_config.execution.mode == "maker_fallback"


def test_patch_with_no_changes_returns_empty_diff(cfg_file):
    w = ConfigWriter(cfg_file)
    result = w.patch_and_save({
        "execution.mode": "taker",     # same as existing
    })
    assert result.changed_keys == []


def test_patch_rejects_invalid_value(cfg_file):
    """Sending a non-numeric `amount_aed` should be caught before disk write."""
    w = ConfigWriter(cfg_file)
    # Stash original contents
    before = cfg_file.read_text()
    with pytest.raises(ConfigWriteError):
        w.patch_and_save({"strategy.amount_aed": "not-a-number"})
    # File on disk untouched
    assert cfg_file.read_text() == before


def test_atomic_write_no_partial_file(cfg_file, tmp_path):
    """If patch_and_save succeeds, no .tmp file is left behind."""
    w = ConfigWriter(cfg_file)
    w.patch_and_save({"strategy.amount_aed": "999"})
    tmp_files = list(tmp_path.glob(".config.*.tmp"))
    assert tmp_files == [], f"leftover temp files: {tmp_files}"


def test_creates_nested_section_for_new_overlay(cfg_file):
    """Adding a new overlay section that didn't exist in the file works."""
    w = ConfigWriter(cfg_file)
    result = w.patch_and_save({
        "overlays.buy_the_dip.enabled": True,
        "overlays.buy_the_dip.threshold_pct": "-15",
    })
    assert "overlays.buy_the_dip.enabled" in result.changed_keys
    raw = yaml.safe_load(cfg_file.read_text())
    assert raw["overlays"]["buy_the_dip"]["enabled"] is True
