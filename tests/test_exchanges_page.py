"""
Regression test for the /exchanges dashboard page.

Bug (2026-06-02): exchanges_page called `required_fields.get(ex)`, treating
the `required_fields()` function as if it were a dict. This raised
`AttributeError: 'function' object has no attribute 'get'` and returned a
500 — the "black screen" Ben saw in prod.

The crash only fired when the SecretStore was active (DCA_SECRETS_KEY set),
which is exactly the hosted-prod condition — so self-host/dev never hit it.
This test pins that path: build the real app with a SecretStore configured
and assert /exchanges renders 200.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.persistence.secrets import required_fields
from bitcoiners_dca.utils.config import AppConfig
from bitcoiners_dca.web.dashboard import create_app


@pytest.fixture
def client_with_secretstore(monkeypatch):
    """Real dashboard app with the SecretStore active — the prod condition
    that triggered the /exchanges 500."""
    monkeypatch.setenv("DCA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = os.path.join(tempfile.mkdtemp(), "test.db")
    config = AppConfig()
    config.persistence.db_path = db_path
    app = create_app(config=config, db=Database(db_path))
    return TestClient(app)


def test_exchanges_page_renders_with_secretstore(client_with_secretstore):
    resp = client_with_secretstore.get("/exchanges")
    assert resp.status_code == 200
    assert "Exchanges" in resp.text


def test_required_fields_is_callable_not_a_dict():
    # The bug was a type confusion: required_fields is a function, not a
    # dict. Calling .get(ex) on it raised AttributeError. Pin the contract.
    assert callable(required_fields)
    for ex in ("okx", "binance", "bitoasis"):
        fields = required_fields(ex)
        assert isinstance(fields, list) and fields
