"""
Dashboard security guards.

Withdrawal capability was REMOVED from the app 2026-08-24 (Ben's directive
after the tenant-dashboard breach). The endpoint-level withdraw-now tests
that used to live here are replaced by `test_withdrawal_endpoints_are_removed`,
which pins that removal so it can't silently regress. The `_redact_exchange_error`
and strategy-save validator tests are retained — those surfaces still exist.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from bitcoiners_dca.core.models import Withdrawal, WithdrawalStatus
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.utils.config import AppConfig
from bitcoiners_dca.web.dashboard import _redact_exchange_error, create_app

BTC_ADDR = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


class StubExchange:
    """Generic self-host exchange stub (no gate) for endpoint tests."""
    name = "okx"
    dry_run = False

    def __init__(self):
        self.calls = 0

    async def withdraw_btc(self, amount_btc, address, network="bitcoin",
                           rcvr_info=None):
        # Retained only as dormant adapter plumbing; the app never calls it.
        self.calls += 1
        return Withdrawal(
            exchange=self.name, withdrawal_id=f"w-{self.calls}",
            asset="BTC", amount=Decimal(str(amount_btc)), address=address,
            fee=Decimal("0.0002"), status=WithdrawalStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )


def _client(stub=None) -> tuple[TestClient, Database]:
    db = Database(os.path.join(tempfile.mkdtemp(), "w.db"))
    config = AppConfig()
    app = create_app(config=config, db=db, exchanges=[stub or StubExchange()])
    return TestClient(app), db


# ─── withdrawal capability is gone ─────────────────────────────────────


def test_withdrawal_endpoints_are_removed():
    """No web surface may move BTC. Every former withdrawal route 404s and
    the adapter's withdraw_btc must never be reached."""
    stub = StubExchange()
    client, _db = _client(stub)

    # POST money-movement endpoint.
    r = client.post("/withdrawals/withdraw-now", data={
        "exchange": "okx", "destination": BTC_ADDR, "amount_btc": "0.01",
    })
    assert r.status_code == 404, r.status_code
    # GET page + supporting APIs.
    for path in ("/withdrawals",
                 "/api/withdrawable-btc?ex=okx",
                 "/api/withdrawal-destinations?ex=okx"):
        assert client.get(path).status_code == 404, path
    # The adapter withdraw path was never invoked.
    assert stub.calls == 0


def test_nav_has_no_withdrawals_link():
    client, _db = _client()
    body = client.get("/").text
    assert "/withdrawals" not in body
    assert "Withdrawals" not in body


# ─── _redact_exchange_error unit behaviour (still used by test/buy/tg) ──


def test_redact_strips_secret_kv_pairs():
    msg = _redact_exchange_error(
        RuntimeError("okx 401: api_key=abc123SECRETxyz890longtoken sign: ZZZ")
    )
    assert "abc123SECRET" not in msg
    assert "[redacted]" in msg


def test_redact_strips_long_tokens_but_keeps_error_codes():
    token = "A" * 32
    msg = _redact_exchange_error(
        RuntimeError(f"50110: IP not whitelisted (token {token})")
    )
    assert token not in msg
    assert "50110" in msg
    assert "IP not whitelisted" in msg


def test_redact_truncates():
    msg = _redact_exchange_error(RuntimeError("x" * 500), limit=100)
    assert len(msg) <= 100


# ─── audit 2026-06-10 P2: strategy-save validator gaps ─────────────────


def _strategy_form(budget: str) -> dict:
    return {"frequency": "weekly", "budget_period": "monthly",
            "budget_amount": budget, "time": "09:00",
            "timezone": "Asia/Dubai", "day_of_week": "monday"}


def test_strategy_save_rejects_nan_and_infinity_budget():
    client, _db = _client()
    for bad in ("NaN", "Infinity", "-5", "0"):
        r = client.post("/strategy", data=_strategy_form(bad))
        assert r.status_code == 200
        assert "must be a positive number" in r.text, bad


def test_strategy_save_clamps_dip_multiplier(tmp_path):
    """A form bypass posting dip_multiplier=100 must persist a clamped
    value — the dip field was the one overlay multiplier with no
    server-side clamp (audit 2026-06-10 P2)."""
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("strategy:\n  amount_aed: '100'\n")
    db = Database(os.path.join(tempfile.mkdtemp(), "s.db"))
    app = create_app(config_path=str(cfg_path), db=db, exchanges=[StubExchange()])
    client = TestClient(app)

    form = _strategy_form("1000")
    form["dip_enabled"] = "on"
    form["dip_multiplier"] = "100"          # form bypass
    r = client.post("/strategy", data=form)
    assert r.status_code == 200

    saved = yaml.safe_load(cfg_path.read_text())
    mult = Decimal(str(saved["overlays"]["buy_the_dip"]["multiplier"]))
    assert mult <= Decimal("5")
