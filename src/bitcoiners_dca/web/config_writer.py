"""
Atomic, validated config.yaml writes for the customer dashboard.

Customer edits fields in the dashboard → POST handler calls
`ConfigWriter.patch_and_save(updates)` → we:

  1. Load the current YAML
  2. Apply `updates` (dotted-path → value) onto a working copy
  3. Validate the result against `AppConfig` (Pydantic)
  4. Write to a sibling temp file in the same directory
  5. fsync the temp file
  6. Atomic rename over the original

If validation fails, we raise WITHOUT touching the on-disk file. The
dashboard surfaces the error to the customer.

Why this matters: an unvalidated config write could brick the daemon (next
cycle crashes on load_config). Pydantic validation up front prevents that.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bitcoiners_dca.utils.config import AppConfig


class ConfigWriteError(ValueError):
    """Raised when a proposed patch produces an invalid AppConfig."""


@dataclass
class ConfigPatchResult:
    """What changed after `patch_and_save()`. Useful for audit logs."""
    changed_keys: list[str]
    new_config: AppConfig


class ConfigWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Config not found: {self.path}")

    def _load_raw(self) -> dict:
        with self.path.open() as f:
            return yaml.safe_load(f) or {}

    def patch_and_save(self, updates: dict[str, Any]) -> ConfigPatchResult:
        """Apply dotted-path updates, validate, atomically write.

        Example updates:
            {
              "strategy.amount_aed": 750,
              "execution.mode": "maker_fallback",
              "overlays.buy_the_dip.enabled": True,
            }

        Raises ConfigWriteError if the result fails Pydantic validation.
        """
        raw = self._load_raw()
        changed: list[str] = []
        for dotted, value in updates.items():
            old = _get_dotted(raw, dotted)
            if old != value:
                _set_dotted(raw, dotted, value)
                changed.append(dotted)

        if not changed:
            # No-op — return existing parsed config without disk writes
            return ConfigPatchResult(
                changed_keys=[], new_config=AppConfig.model_validate(raw),
            )

        # Validate the proposed shape
        try:
            new_cfg = AppConfig.model_validate(raw)
        except Exception as e:
            raise ConfigWriteError(f"Patched config failed validation: {e}") from e

        # Atomic write: temp → fsync → rename
        dir_ = self.path.parent
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_, prefix=".config.", suffix=".yaml.tmp",
            delete=False, encoding="utf-8",
        ) as tmp:
            yaml.safe_dump(raw, tmp, sort_keys=False, default_flow_style=False)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        # Replace atomic on POSIX
        os.replace(tmp_path, self.path)
        return ConfigPatchResult(changed_keys=changed, new_config=new_cfg)


# --- dotted-path helpers ---

def _get_dotted(d: dict, path: str) -> Any:
    """Walk dotted path into nested dict. Returns None if any segment missing."""
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_dotted(d: dict, path: str, value: Any) -> None:
    """Set value at dotted path, creating intermediate dicts as needed."""
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
