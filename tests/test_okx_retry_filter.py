"""
Regression: the OKX adapter's `_create_market_buy_only` / `place_limit_buy`
retry decorator must ONLY retry on the network-level exception set that
fires BEFORE the request hits the exchange. Retrying on a generic
`ExchangeError` is unsafe — the order may have been placed server-side
even though the client raised, and a retry would create a duplicate.

This test pins the membership of `_SAFE_RETRY_EXCEPTIONS` so a refactor
that adds (say) ccxt.InvalidOrder to the list will trip the test.
"""
from __future__ import annotations

import ccxt.async_support as ccxt_async
import pytest

from bitcoiners_dca.exchanges.base import ExchangeError, InsufficientBalanceError
from bitcoiners_dca.exchanges.okx import _SAFE_RETRY_EXCEPTIONS


def test_safe_retry_set_is_network_layer_only():
    safe = set(_SAFE_RETRY_EXCEPTIONS)
    expected = {
        ccxt_async.NetworkError,
        ccxt_async.RequestTimeout,
        ccxt_async.DDoSProtection,
        ccxt_async.RateLimitExceeded,
    }
    assert safe == expected, (
        f"_SAFE_RETRY_EXCEPTIONS drifted: extra={safe - expected}, "
        f"missing={expected - safe}. Adding a non-network exception here "
        f"risks duplicate orders on retry."
    )


@pytest.mark.parametrize(
    "exc_type",
    [ExchangeError, InsufficientBalanceError, ccxt_async.InvalidOrder, ccxt_async.AuthenticationError],
)
def test_unsafe_exceptions_not_retryable(exc_type):
    """These exceptions either (a) prove the order partially landed
    server-side, or (b) won't be fixed by a retry. They must NOT be in
    the safe-retry set."""
    assert not issubclass(exc_type, _SAFE_RETRY_EXCEPTIONS), (
        f"{exc_type.__name__} would be retried under current "
        f"_SAFE_RETRY_EXCEPTIONS — risk of duplicate orders."
    )


@pytest.mark.parametrize(
    "exc_type",
    [
        ccxt_async.NetworkError,
        ccxt_async.RequestTimeout,
        ccxt_async.DDoSProtection,
        ccxt_async.RateLimitExceeded,
    ],
)
def test_safe_exceptions_are_retryable(exc_type):
    """These exceptions fire client-side before any request reaches the
    exchange (or — for rate limits — explicitly invite retry). They are
    safe to retry."""
    assert issubclass(exc_type, _SAFE_RETRY_EXCEPTIONS), (
        f"{exc_type.__name__} not in _SAFE_RETRY_EXCEPTIONS — "
        f"transient network errors would no longer auto-retry."
    )
