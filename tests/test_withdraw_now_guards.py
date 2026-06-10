"""
Audit 2026-06-10 P1 batch C — withdraw-now moves real BTC, so it gets:

  1. an in-flight lock (second request while one is talking to the
     exchange → rejected, no second transfer);
  2. a 2-minute same-exchange cooldown backed by the withdrawals table —
     which previously had ZERO writers, so the audit trail and the
     existing recent_withdrawal_exists() idempotency gate were dead;
  3. persisted Withdrawal rows;
  4. redacted error surfaces (exchange exceptions can echo request
     details, incl. the API key, into the response/URL).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import bitcoiners_dca.web.dashboard as dashboard_module
from bitcoiners_dca.core.models import Withdrawal, WithdrawalStatus
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.utils.config import AppConfig
from bitcoiners_dca.web.dashboard import _redact_exchange_error, create_app

BTC_ADDR = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


class StubWithdrawExchange:
    name = "okx"
    dry_run = False

    def __init__(self, fail_with: Exception | None = None):
        self.calls = 0
        self._fail_with = fail_with

    async def withdraw_btc(self, amount_btc, address, network="bitcoin",
                           rcvr_info=None):
        self.calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        return Withdrawal(
            exchange=self.name, withdrawal_id=f"w-{self.calls}",
            asset="BTC", amount=Decimal(str(amount_btc)), address=address,
            fee=Decimal("0.0002"), status=WithdrawalStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )


def _client(stub) -> tuple[TestClient, Database]:
    db = Database(os.path.join(tempfile.mkdtemp(), "w.db"))
    config = AppConfig()
    app = create_app(config=config, db=db, exchanges=[stub])
    return TestClient(app), db


def _post(client: TestClient, amount="0.01"):
    return client.post("/withdrawals/withdraw-now", data={
        "exchange": "okx", "destination": BTC_ADDR, "amount_btc": amount,
    })


def test_withdrawal_is_persisted_and_cooldown_blocks_resubmit():
    stub = StubWithdrawExchange()
    client, db = _client(stub)

    r1 = _post(client)
    assert r1.status_code == 200
    assert "Withdrawal submitted" in r1.text
    assert stub.calls == 1
    # Persisted — the table previously had zero writers.
    assert db.recent_withdrawal_exists("okx", "BTC", since_minutes=2)

    # An immediate resubmit (double-click after the first returned,
    # browser back-button repost, …) is treated as the same intent.
    r2 = _post(client)
    assert "less than" in r2.text and "2 minutes" in r2.text
    assert stub.calls == 1   # no second transfer


def test_in_flight_lock_rejects_concurrent_submit():
    stub = StubWithdrawExchange()
    client, _db = _client(stub)

    # Simulate a request currently holding the lock (mid-exchange-call).
    asyncio.run(dashboard_module._withdraw_in_flight.acquire())
    try:
        r = _post(client)
        assert "already in flight" in r.text
        assert stub.calls == 0
    finally:
        dashboard_module._withdraw_in_flight.release()


def test_failed_withdrawal_error_is_redacted():
    secret = "AKIA" + "x" * 40
    stub = StubWithdrawExchange(
        fail_with=RuntimeError(f"signature rejected for apiKey={secret}")
    )
    client, db = _client(stub)

    r = _post(client)
    assert "rejected the withdrawal" in r.text
    assert secret not in r.text
    assert "redacted" in r.text
    # Nothing persisted on failure.
    assert not db.recent_withdrawal_exists("okx", "BTC", since_minutes=2)


# ─── _redact_exchange_error unit behaviour ─────────────────────────────


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
