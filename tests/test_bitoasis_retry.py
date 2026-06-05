"""
BitOasis transient-5xx retry behaviour.

BitOasis sits behind Cloudflare; when its origin is overloaded the API
returns a Cloudflare 502 ("bad gateway") — intermittently, then clears.
Before the fix these surfaced as immediate `health check failed` alerts
because `_request_with_retry` only retried network/timeout/429. These
tests pin the contract: transient 5xx on an idempotent read is retried
and recovers silently; a sustained 5xx still raises (real outage → alert).
"""
from __future__ import annotations

import httpx
import pytest

from bitcoiners_dca.exchanges.base import ExchangeError
from bitcoiners_dca.exchanges.bitoasis import (
    BitOasisExchange,
    BitOasisServerError,
)


def _exchange_with_responses(statuses: list[int]) -> BitOasisExchange:
    """Build a BitOasis adapter whose HTTP client replays `statuses` in order.

    The last status repeats once the list is exhausted, so a single-element
    list models a permanently-failing origin.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        code = statuses[i]
        if code == 200:
            return httpx.Response(200, json={"balances": []})
        return httpx.Response(code, text="Error 502: Bad gateway")

    ex = BitOasisExchange(api_token="dummy")
    ex._client = httpx.AsyncClient(
        base_url="https://mock.bitoasis",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    ex._handler_calls = calls  # type: ignore[attr-defined]
    return ex


@pytest.mark.asyncio
@pytest.mark.parametrize("transient_code", [500, 502, 503, 504])
async def test_health_check_recovers_after_transient_5xx(transient_code: int):
    """A single transient 5xx then 200 → health check passes (no alert)."""
    ex = _exchange_with_responses([transient_code, 200])
    try:
        assert await ex.health_check() is True
        # Proves the retry actually fired: the failing attempt + the good one.
        assert ex._handler_calls["n"] == 2  # type: ignore[attr-defined]
    finally:
        await ex._client.aclose()


@pytest.mark.asyncio
async def test_health_check_raises_on_sustained_5xx():
    """A 502 on every attempt → retries exhaust → ExchangeError (real outage)."""
    ex = _exchange_with_responses([502])
    try:
        with pytest.raises(ExchangeError) as exc:
            await ex.health_check()
        assert "502" in str(exc.value)
        # RETRY_ATTEMPTS = 3 → the origin was hit three times before giving up.
        assert ex._handler_calls["n"] == 3  # type: ignore[attr-defined]
    finally:
        await ex._client.aclose()


@pytest.mark.asyncio
async def test_transient_5xx_classified_as_server_error():
    """5xx maps to the retryable BitOasisServerError, not a generic error."""
    ex = _exchange_with_responses([503])
    try:
        with pytest.raises(BitOasisServerError):
            await ex._request("GET", "/exchange/balances")
    finally:
        await ex._client.aclose()


@pytest.mark.asyncio
async def test_non_transient_4xx_not_retried():
    """A 400 is a real client error — surface immediately, no retry."""
    ex = _exchange_with_responses([400])
    try:
        with pytest.raises(ExchangeError) as exc:
            await ex._request_with_retry("GET", "/exchange/balances")
        assert not isinstance(exc.value, BitOasisServerError)
        assert ex._handler_calls["n"] == 1  # type: ignore[attr-defined]
    finally:
        await ex._client.aclose()
