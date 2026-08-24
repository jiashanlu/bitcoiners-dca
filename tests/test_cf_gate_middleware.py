"""
Ingress gate middleware tests (hardened 2026-08-24 after the breach).

The dashboard no longer trusts the `cf-access-authenticated-user-email`
header on its own — that header is forgeable by anyone who can reach the
origin. Hosted mode now requires an unforgeable proof-of-origin secret
(`x-dca-proxy-secret`) that only the bitcoiners-app proxy knows, and fails
CLOSED when the owner email or the secret is unconfigured.

The middleware enforces, in hosted mode (DCA_REQUIRE_CF_HEADER=1):
  1. Valid proof-of-origin secret, else 403.
  2. cf-access-authenticated-user-email present, else 401.
  3. Email matches DCA_TENANT_OWNER_EMAIL (case-insensitive, trimmed), else 403.
  4. Owner or secret unset → 503 (fail closed, never allow).
  5. /healthz always allowed.
  6. Self-host (env unset) → gate skipped.

Audit B-P1-6 2026-05-21; hardened after [[dca_tenant_dashboard_breach_2026_08]].
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bitcoiners_dca.web.dashboard import (
    CF_USER_HEADER, PROXY_SECRET_HEADER, _CFGateMiddleware,
)

SECRET = "proxy-shared-secret-value-32bytes-long"


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


def _set_env(monkeypatch, require_cf: bool, tenant_owner: str | None,
             secret: str | None = SECRET):
    if require_cf:
        monkeypatch.setenv("DCA_REQUIRE_CF_HEADER", "1")
    else:
        monkeypatch.delenv("DCA_REQUIRE_CF_HEADER", raising=False)
    if tenant_owner is not None:
        monkeypatch.setenv("DCA_TENANT_OWNER_EMAIL", tenant_owner)
    else:
        monkeypatch.delenv("DCA_TENANT_OWNER_EMAIL", raising=False)
    if secret is not None:
        monkeypatch.setenv("DCA_PROXY_SHARED_SECRET", secret)
    else:
        monkeypatch.delenv("DCA_PROXY_SHARED_SECRET", raising=False)


def _hdr(cf_email: str, secret: str = SECRET) -> dict:
    return {PROXY_SECRET_HEADER: secret, CF_USER_HEADER: cf_email}


def test_healthz_always_allowed(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    assert TestClient(_app()).get("/healthz").status_code == 200


def test_forged_header_without_secret_is_403(monkeypatch):
    # The 2026-08 exploit shape: forged identity header, no proxy secret.
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    r = TestClient(_app()).get("/", headers={CF_USER_HEADER: "owner@example.com"})
    assert r.status_code == 403


def test_missing_cf_header_is_401_when_secret_present(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    r = TestClient(_app()).get("/", headers={PROXY_SECRET_HEADER: SECRET})
    assert r.status_code == 401
    assert "missing proxy header" in r.text.lower()


def test_missing_everything_allowed_when_not_required(monkeypatch):
    _set_env(monkeypatch, require_cf=False, tenant_owner=None, secret=None)
    assert TestClient(_app()).get("/").status_code == 200


def test_cf_header_matches_tenant_owner_allowed(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    r = TestClient(_app()).get("/", headers=_hdr("owner@example.com"))
    assert r.status_code == 200


def test_cf_header_mismatch_blocked(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    r = TestClient(_app()).get("/", headers=_hdr("attacker@example.com"))
    assert r.status_code == 403
    assert "different account" in r.text.lower()


def test_cf_header_case_insensitive(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="OWNER@example.com")
    r = TestClient(_app()).get("/", headers=_hdr("owner@EXAMPLE.com"))
    assert r.status_code == 200


def test_tenant_owner_unset_fails_closed(monkeypatch):
    """Hosted mode with the owner gate unconfigured must REFUSE (was the
    silent-allow that enabled the breach), not warn-and-allow."""
    _set_env(monkeypatch, require_cf=True, tenant_owner=None)
    r = TestClient(_app()).get("/", headers=_hdr("anyone@example.com"))
    assert r.status_code == 503


def test_proxy_secret_unset_fails_closed(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com", secret=None)
    r = TestClient(_app()).get("/", headers={CF_USER_HEADER: "owner@example.com"})
    assert r.status_code == 503


def test_cf_header_with_whitespace_normalised(monkeypatch):
    _set_env(monkeypatch, require_cf=True, tenant_owner="owner@example.com")
    r = TestClient(_app()).get("/", headers=_hdr("  owner@example.com  "))
    assert r.status_code == 200
