"""
CF-Access gate middleware tests. The dashboard sits behind CF Access
in production — a mis-scoped CF Access policy (wildcard email, wrong
group, broken include rule) would otherwise let any authenticated CF
user reach any tenant's dashboard.

The middleware enforces:
  1. If DCA_REQUIRE_CF_HEADER=1, requests without cf-access-authenticated-
     user-email are 401.
  2. If DCA_TENANT_OWNER_EMAIL is set, the CF email MUST match it,
     case-insensitive. Cross-tenant attempts get 403.
  3. /healthz is always allowed.
  4. Self-hosted installs (env unset) still work — gate skipped with
     a one-time warning.

Audit B-P1-6 2026-05-21.
"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient
from fastapi import FastAPI

from bitcoiners_dca.web.dashboard import _CFGateMiddleware


def _app() -> FastAPI:
    a = FastAPI()
    a.add_middleware(_CFGateMiddleware)

    @a.get("/")
    def _root():
        return {"ok": True}

    @a.get("/healthz")
    def _hz():
        return {"healthz": "ok"}

    return a


def _set_env(monkeypatch, require_cf: bool, tenant_owner: str | None):
    if require_cf:
        monkeypatch.setenv("DCA_REQUIRE_CF_HEADER", "1")
    else:
        monkeypatch.delenv("DCA_REQUIRE_CF_HEADER", raising=False)
    if tenant_owner is not None:
        monkeypatch.setenv("DCA_TENANT_OWNER_EMAIL", tenant_owner)
    else:
        monkeypatch.delenv("DCA_TENANT_OWNER_EMAIL", raising=False)
    # Reset the once-flag so the warning message logic can be tested fresh.
    _CFGateMiddleware._owner_email_logged_once = False


def test_healthz_always_allowed(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    c = TestClient(_app())
    r = c.get("/healthz")
    assert r.status_code == 200


def test_missing_cf_header_is_401_when_required(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner=None)
    c = TestClient(_app())
    r = c.get("/")
    assert r.status_code == 401
    assert "missing proxy header" in r.text.lower()


def test_missing_cf_header_allowed_when_not_required(monkeypatch):
    _set_env(monkeypatch, require_cf=False, tenant_owner=None)
    c = TestClient(_app())
    r = c.get("/")
    assert r.status_code == 200


def test_cf_header_matches_tenant_owner_allowed(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    c = TestClient(_app())
    r = c.get(
        "/",
        headers={"cf-access-authenticated-user-email": "owner@example.com"},
    )
    assert r.status_code == 200


def test_cf_header_mismatch_blocked(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    c = TestClient(_app())
    r = c.get(
        "/",
        headers={"cf-access-authenticated-user-email": "attacker@example.com"},
    )
    assert r.status_code == 403
    assert "different account" in r.text.lower()


def test_cf_header_case_insensitive(monkeypatch):
    """Owner email comparison must be case-insensitive — Gmail capitalises
    the first letter on some auto-fill paths."""
    _set_env(monkeypatch, require_cf=True, tenant_owner="OWNER@example.com")
    c = TestClient(_app())
    r = c.get(
        "/",
        headers={"cf-access-authenticated-user-email": "owner@EXAMPLE.com"},
    )
    assert r.status_code == 200


def test_tenant_owner_unset_skipped_with_warning(monkeypatch, caplog):
    """Self-hosted installs leave DCA_TENANT_OWNER_EMAIL unset. The
    middleware must NOT block them — just warn once."""
    import logging
    _set_env(monkeypatch, require_cf=True, tenant_owner=None)
    c = TestClient(_app())
    with caplog.at_level(logging.WARNING):
        r = c.get(
            "/",
            headers={"cf-access-authenticated-user-email": "anyone@example.com"},
        )
    assert r.status_code == 200
    # Warning should fire on first call.
    assert any("DCA_TENANT_OWNER_EMAIL" in rec.message for rec in caplog.records)


def test_cf_header_with_whitespace_normalised(monkeypatch):
    """Email comparison must strip whitespace — some CF Access edge
    configurations have a trailing space in the header."""
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    c = TestClient(_app())
    r = c.get(
        "/",
        headers={"cf-access-authenticated-user-email": "  owner@example.com  "},
    )
    assert r.status_code == 200
