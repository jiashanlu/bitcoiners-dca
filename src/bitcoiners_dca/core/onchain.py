"""
On-chain market signal fetcher — talks to a Bitcoin Research Kit (BRK) HTTP
server. Default base URL is the public bitview.space instance; set
`BRK_API_BASE` to point at your own self-hosted BRK node when you stand
one up.

Surface used:
  GET /api/series/{metric}/{index}/latest  → scalar JSON number
  GET /api/series/{metric}/{index}         → { version, data: [...], ... }

Metrics we read today (all keyed at the `day1` index):
  - mvrv                         classic Market-value/Realized-value ratio
  - realized_price_ratio_zscore  BRK's MVRV-Z analogue (all-time z-score
                                 of price / realized-price)
  - sopr_1w                      1-week Spent-Output-Profit-Ratio
  - pi_cycle                     Pi-Cycle Top indicator (1.0 = signal)

The overlay only reads `latest` — cheap call, single float. Values are
cached in-process for `ttl_seconds` so back-to-back cycles inside the
TTL hit memory not the network.

The bot must keep DCA'ing even when this data source is down. All
errors raise `OnchainSignalError`; the strategy treats that as "no
multiplier" and continues with the base amount.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://bitview.space"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_TTL_S = 3600  # day1 metrics don't move within an hour
# bitview.space 403s requests with the default httpx/urllib UA. Use a
# real-browser-ish UA + identify ourselves with a contact suffix so they
# can rate-limit us specifically if they want to.
_UA = "Mozilla/5.0 (compatible; bitcoiners-dca/1.0; +https://bitcoiners.ae)"

SUPPORTED_METRICS: dict[str, str] = {
    # Internal name → BRK series ID
    "mvrv": "mvrv",
    "mvrv_z": "realized_price_ratio_zscore",
    "sopr_1w": "sopr_1w",
    "pi_cycle": "pi_cycle",
}


class OnchainSignalError(RuntimeError):
    """Raised when the BRK API can't be reached or the response is bad."""


@dataclass
class _CacheEntry:
    value: Decimal
    fetched_at: float


class OnchainClient:
    """Tiny BRK HTTP client with per-process TTL cache.

    Construct one per process; share across cycles. Thread-safe in the
    sense asyncio expects — concurrent `get()` calls for the same metric
    coalesce on a single in-flight request via the per-metric lock.
    """

    def __init__(self, base_url: Optional[str] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 ttl_s: int = DEFAULT_TTL_S):
        self.base_url = (base_url or os.getenv("BRK_API_BASE", DEFAULT_BASE)).rstrip("/")
        self.timeout_s = timeout_s
        self.ttl_s = ttl_s
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, metric: str, index: str = "day1") -> Decimal:
        if metric not in SUPPORTED_METRICS:
            raise OnchainSignalError(f"Unsupported metric '{metric}'. "
                                     f"Supported: {sorted(SUPPORTED_METRICS)}")
        series = SUPPORTED_METRICS[metric]
        cache_key = f"{series}/{index}"

        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached.fetched_at) < self.ttl_s:
            return cached.value

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            # Re-check under lock (concurrent waiters).
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached.fetched_at) < self.ttl_s:
                return cached.value

            value = await self._fetch(series, index)
            self._cache[cache_key] = _CacheEntry(value=value, fetched_at=time.time())
            return value

    async def _fetch(self, series: str, index: str) -> Decimal:
        url = f"{self.base_url}/api/series/{series}/{index}/latest"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s,
                headers={"User-Agent": _UA, "Accept": "application/json"},
            ) as client:
                resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text.strip()
            # BRK returns a bare JSON number, e.g. "1.4156999588012695".
            return Decimal(text)
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("BRK %s/%s fetch failed: %s", series, index, e)
            raise OnchainSignalError(f"BRK fetch failed for {series}/{index}: {e}") from e


_default_client: Optional[OnchainClient] = None


def get_default_client() -> OnchainClient:
    global _default_client
    if _default_client is None:
        _default_client = OnchainClient()
    return _default_client
