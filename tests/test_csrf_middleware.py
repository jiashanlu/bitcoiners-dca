"""
CSRF (same-site origin) middleware tests. The dashboard rides behind a
CF Access JWT in production — a browser-driven CSRF would let any
attacker page POST as the user with their cookies auto-sent. The
middleware blocks any POST whose `Origin` or `Referer` doesn't match
the host (or the X-Forwarded-* equivalent).
"""
from fastapi.testclient import TestClient
from fastapi import FastAPI

from bitcoiners_dca.web.dashboard import _OriginCSRFMiddleware


def _app():
    a = FastAPI()
    a.add_middleware(_OriginCSRFMiddleware)

    @a.get("/get")
    def _g(): return {"ok": True}

    @a.post("/post")
    def _p(): return {"ok": True}

    return a


def test_get_always_allowed():
    c = TestClient(_app())
    r = c.get("/get")
    assert r.status_code == 200


def test_post_with_matching_origin_allowed():
    c = TestClient(_app())
    r = c.post("/post", headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_post_with_attacker_origin_blocked():
    c = TestClient(_app())
    r = c.post("/post", headers={"origin": "https://attacker.example"})
    assert r.status_code == 403
    assert r.json()["error"] == "csrf"


def test_post_with_referer_under_origin_allowed():
    c = TestClient(_app())
    r = c.post("/post", headers={"referer": "http://testserver/anything"})
    assert r.status_code == 200


def test_post_with_attacker_referer_blocked():
    c = TestClient(_app())
    r = c.post("/post", headers={"referer": "https://attacker.example/page"})
    assert r.status_code == 403


def test_post_with_no_headers_allowed_for_curl():
    """Server-to-server / curl callers don't send Origin/Referer; allow them.
    Sufficient because the bigger gate is CF Access JWT in production."""
    c = TestClient(_app())
    r = c.post("/post")
    assert r.status_code == 200


def test_post_with_xforwarded_host_matches_origin():
    """When proxied behind bitcoiners-app, X-Forwarded-Host is the user-
    facing hostname. Origin should match that, not the upstream."""
    c = TestClient(_app())
    r = c.post(
        "/post",
        headers={
            "origin": "https://app.bitcoiners.ae",
            "x-forwarded-host": "app.bitcoiners.ae",
            "x-forwarded-proto": "https",
        },
    )
    assert r.status_code == 200


def test_cf_access_header_without_origin_or_referer_allowed():
    """Server-to-server style call via the bitcoiners-app proxy — the
    proxy sets cf-access-authenticated-user-email and the upstream
    HTMX/fetch chain may strip Origin. Should pass (the proxy already
    validated the session)."""
    c = TestClient(_app())
    r = c.post(
        "/post",
        headers={"cf-access-authenticated-user-email": "ben@example.com"},
    )
    assert r.status_code == 200


def test_cf_access_header_with_matching_origin_allowed():
    c = TestClient(_app())
    r = c.post(
        "/post",
        headers={
            "cf-access-authenticated-user-email": "ben@example.com",
            "origin": "https://app.bitcoiners.ae",
            "x-forwarded-host": "app.bitcoiners.ae",
        },
    )
    assert r.status_code == 200


def test_cf_access_header_with_attacker_origin_blocked():
    """Defense-in-depth: even with a valid CF Access header set by the
    proxy, a same-site CSRF from attacker.example must still be
    rejected if it somehow reaches the dashboard. The CF header alone
    is no longer a get-out-of-jail card."""
    c = TestClient(_app())
    r = c.post(
        "/post",
        headers={
            "cf-access-authenticated-user-email": "ben@example.com",
            "origin": "https://attacker.example",
        },
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "cf-access-authenticated header present" in detail


def test_cf_access_header_with_attacker_referer_blocked():
    c = TestClient(_app())
    r = c.post(
        "/post",
        headers={
            "cf-access-authenticated-user-email": "ben@example.com",
            "referer": "https://attacker.example/page",
        },
    )
    assert r.status_code == 403
