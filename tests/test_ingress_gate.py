"""
Ingress gate + CSRF hardening (2026-08-24, post-breach).

The breach: the dashboard trusted a plaintext, forgeable
`Cf-Access-Authenticated-User-Email` header as identity, and the CSRF layer
allowed header-only requests with no Origin/Referer. Fix: hosted mode now
requires an unforgeable proof-of-origin secret that only the app proxy knows,
fails closed on misconfiguration, and CSRF keys on that secret instead of the
identity header.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.utils.config import AppConfig
from bitcoiners_dca.web.dashboard import (
    CF_USER_HEADER, PROXY_SECRET_HEADER, create_app,
)

SECRET = "proxy-shared-secret-value-32bytes-long"
OWNER = "owner@example.com"


def _client() -> TestClient:
    db = Database(os.path.join(tempfile.mkdtemp(), "g.db"))
    return TestClient(create_app(config=AppConfig(), db=db, exchanges=[]),
                      raise_server_exceptions=False)


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("DCA_REQUIRE_CF_HEADER", "1")
    monkeypatch.setenv("DCA_PROXY_SHARED_SECRET", SECRET)
    monkeypatch.setenv("DCA_TENANT_OWNER_EMAIL", OWNER)
    return _client()


def test_forged_identity_header_without_secret_is_rejected(hosted):
    # The exact 2026-08 exploit shape: curl with a forged owner email,
    # no Origin, no proxy secret.
    r = hosted.get("/", headers={CF_USER_HEADER: OWNER})
    assert r.status_code == 403


def test_valid_proxy_secret_and_owner_is_allowed(hosted):
    r = hosted.get("/", headers={PROXY_SECRET_HEADER: SECRET, CF_USER_HEADER: OWNER})
    assert r.status_code == 200


def test_secret_present_but_missing_identity_is_401(hosted):
    r = hosted.get("/", headers={PROXY_SECRET_HEADER: SECRET})
    assert r.status_code == 401


def test_wrong_secret_is_rejected(hosted):
    r = hosted.get("/", headers={PROXY_SECRET_HEADER: "nope", CF_USER_HEADER: OWNER})
    assert r.status_code == 403


def test_cross_tenant_identity_is_rejected(hosted):
    r = hosted.get("/", headers={PROXY_SECRET_HEADER: SECRET,
                                 CF_USER_HEADER: "attacker@evil.com"})
    assert r.status_code == 403


def test_fails_closed_when_owner_unset(monkeypatch):
    monkeypatch.setenv("DCA_REQUIRE_CF_HEADER", "1")
    monkeypatch.setenv("DCA_PROXY_SHARED_SECRET", SECRET)
    monkeypatch.delenv("DCA_TENANT_OWNER_EMAIL", raising=False)
    c = _client()
    r = c.get("/", headers={PROXY_SECRET_HEADER: SECRET, CF_USER_HEADER: OWNER})
    assert r.status_code == 503  # refuse, never silently allow


def test_fails_closed_when_secret_unset(monkeypatch):
    monkeypatch.setenv("DCA_REQUIRE_CF_HEADER", "1")
    monkeypatch.delenv("DCA_PROXY_SHARED_SECRET", raising=False)
    monkeypatch.setenv("DCA_TENANT_OWNER_EMAIL", OWNER)
    c = _client()
    r = c.get("/", headers={CF_USER_HEADER: OWNER})
    assert r.status_code == 503


def test_healthz_is_always_reachable(hosted):
    r = hosted.get("/healthz")
    assert r.status_code == 200
    assert "ok" in r.text
    assert "exchange" not in r.text.lower()  # M-3: no exchange disclosure


def test_state_change_without_secret_or_origin_is_csrf_blocked(hosted):
    # Even if the gate were bypassed, CSRF must refuse a no-origin POST.
    # In hosted mode the gate 403s first (no secret) — assert it's blocked.
    r = hosted.post("/controls/pause")
    assert r.status_code in (403, 401)


def test_state_change_with_proxy_secret_passes_csrf(hosted):
    r = hosted.post("/controls/pause",
                    headers={PROXY_SECRET_HEADER: SECRET, CF_USER_HEADER: OWNER})
    # Passes gate + CSRF → handler runs (redirect), never a CSRF 403.
    assert r.status_code != 403


def test_self_host_needs_no_secret(monkeypatch):
    monkeypatch.delenv("DCA_REQUIRE_CF_HEADER", raising=False)
    monkeypatch.delenv("DCA_PROXY_SHARED_SECRET", raising=False)
    c = _client()
    assert c.get("/").status_code == 200
