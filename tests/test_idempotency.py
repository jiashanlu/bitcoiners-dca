"""
Regression tests for the audit P0 money-safety fixes (2026-05-21).

Two retry-creates-duplicate-order classes:

1. OKX: `make_bot_client_order_id()` MUST be called outside the
   `@retry`-wrapped function so each retry sees the SAME clOrdId and
   OKX's server-side duplicate-clientOrderId dedupe catches it (error
   51001, 5-second window). Previously it was generated inside,
   defeating the dedupe.

2. BitOasis: POST /exchange/order MUST NOT be wrapped in
   `_request_with_retry`. BitOasis has no clientOrderId concept (see
   cancel_all_open_orders docstring), so any retry after a server-side
   accept = a second real order = double-spend.

These tests pin both invariants by asserting the call patterns directly,
without trying to mock the entire ccxt/httpx stack.
"""
from __future__ import annotations

import inspect

from bitcoiners_dca.exchanges import okx as okx_module
from bitcoiners_dca.exchanges import bitoasis as bitoasis_module


# ─── OKX: clOrdId generated ONCE per logical buy ──────────────────────────

def test_okx_market_buy_generates_clordid_outside_retry():
    """The outer wrapper `place_market_buy` MUST call
    make_bot_client_order_id() exactly once, then pass the resulting
    id into the @retry-decorated inner. If a future refactor moves the
    call back inside the inner, this test catches it."""
    src = inspect.getsource(okx_module.OKXExchange.place_market_buy)
    assert "make_bot_client_order_id()" in src, (
        "place_market_buy must generate clOrdId BEFORE delegating to the "
        "@retry-wrapped inner function (audit P0 2026-05-21)"
    )
    assert "_create_market_buy_only" in src, (
        "place_market_buy must call _create_market_buy_only(... cl_ord_id)"
    )

    inner_src = inspect.getsource(okx_module.OKXExchange._create_market_buy_only)
    assert "make_bot_client_order_id()" not in inner_src, (
        "_create_market_buy_only must NOT generate clOrdId itself "
        "— that defeats OKX's duplicate-clientOrderId dedupe on retry"
    )
    assert "cl_ord_id" in inner_src, (
        "_create_market_buy_only must accept cl_ord_id as a parameter"
    )


def test_okx_limit_buy_generates_clordid_outside_retry():
    src = inspect.getsource(okx_module.OKXExchange.place_limit_buy)
    assert "make_bot_client_order_id()" in src
    assert "_create_limit_buy_only" in src

    inner_src = inspect.getsource(okx_module.OKXExchange._create_limit_buy_only)
    assert "make_bot_client_order_id()" not in inner_src
    assert "cl_ord_id" in inner_src


# ─── BitOasis: order POST is NOT retry-wrapped ────────────────────────────

def _strip_comments_and_strings(src: str) -> str:
    """Drop python comments and triple-strings — leaves only the code
    we're auditing. The test below has to mention `_request_with_retry`
    in the source-code COMMENT explaining the fix; we want to forbid
    only the CALL site."""
    import re as _re
    out = _re.sub(r"#.*", "", src)
    out = _re.sub(r'"""[\s\S]*?"""', "", out)
    out = _re.sub(r"'''[\s\S]*?'''", "", out)
    return out


def test_bitoasis_market_buy_uses_no_retry_request():
    """The POST /exchange/order path MUST call self._request (single
    attempt, throws on failure) — NOT self._request_with_retry which
    retries on network errors. BitOasis exposes no clientOrderId, so
    a retry after server-side accept doubles the customer's order."""
    src = inspect.getsource(bitoasis_module.BitOasisExchange.place_market_buy)
    code = _strip_comments_and_strings(src)
    assert "_request_with_retry" not in code, (
        "place_market_buy must not CALL _request_with_retry — BitOasis "
        "has no idempotency key, retries would duplicate orders "
        "(audit P0 2026-05-21)"
    )
    assert 'self._request("POST", "/exchange/order"' in src, (
        "place_market_buy must POST via self._request (no retry)"
    )


def test_bitoasis_limit_buy_uses_no_retry_request():
    src = inspect.getsource(bitoasis_module.BitOasisExchange.place_limit_buy)
    code = _strip_comments_and_strings(src)
    assert "_request_with_retry" not in code
    assert 'self._request("POST", "/exchange/order"' in src
