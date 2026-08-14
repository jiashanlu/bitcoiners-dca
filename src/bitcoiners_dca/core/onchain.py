"""
On-chain market signal fetcher — talks to a Bitcoin Research Kit (BRK) HTTP
server. Default base URL is the public bitview.space instance; set
`BRK_API_BASE` to point at your own self-hosted BRK node when you stand
one up.

Surface used:
  GET /api/series/{metric}/{index}/latest  → scalar JSON number
  GET /api/series/{metric}/{index}         → { version, data: [...], ... }

Metrics we read today (all keyed at the `day1` index):
  - mvrv      classic Market-value/Realized-value ratio
  - mvrv_z    all-time z-score of realized_price_ratio, computed HERE from
              the full series. BRK served a precomputed
              `realized_price_ratio_zscore` until ~2026-08 then removed
              every *_zscore series (fetches 404'd and the overlay was
              silently inert). Same statistic, computed client-side.
  - sopr_1w   1-week Spent-Output-Profit-Ratio
  - pi_cycle  Pi-Cycle Top indicator (1.0 = signal)

Scalar metrics read `latest` — cheap call, single float. Computed metrics
read the full series (~55KB for day1 since 2009). Values are cached
in-process for `ttl_seconds` so back-to-back cycles inside the TTL hit
memory not the network.

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
    "sopr_1w": "sopr_1w",
    "pi_cycle": "pi_cycle",
}

# Internal name → BRK series ID whose ALL-TIME Z-SCORE is the metric.
# These fetch the whole series and reduce it locally (BRK no longer
# serves precomputed z-scores).
COMPUTED_ZSCORE_METRICS: dict[str, str] = {
    "mvrv_z": "realized_price_ratio",
}

# Every metric name `OnchainClient.get()` accepts, scalar or computed.
ALL_METRIC_NAMES: frozenset[str] = frozenset(SUPPORTED_METRICS) | frozenset(
    COMPUTED_ZSCORE_METRICS
)


def zscore_of_latest(values: list[Decimal]) -> Decimal:
    """All-time z-score of the last element: (last - mean) / population-std.

    Matches the semantics of BRK's retired *_zscore series (z of today's
    value against the full history). Raises OnchainSignalError on a
    series too short or too flat to standardise — the strategy treats
    that like any other fetch failure (no multiplier, keep DCA'ing).
    """
    if len(values) < 2:
        raise OnchainSignalError("z-score needs at least 2 data points")
    n = Decimal(len(values))
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    if variance == 0:
        raise OnchainSignalError("z-score undefined for a constant series")
    return (values[-1] - mean) / variance.sqrt()


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
        computed = metric in COMPUTED_ZSCORE_METRICS
        if not computed and metric not in SUPPORTED_METRICS:
            supported = sorted([*SUPPORTED_METRICS, *COMPUTED_ZSCORE_METRICS])
            raise OnchainSignalError(f"Unsupported metric '{metric}'. "
                                     f"Supported: {supported}")
        series = (COMPUTED_ZSCORE_METRICS[metric] if computed
                  else SUPPORTED_METRICS[metric])
        cache_key = f"{'zscore:' if computed else ''}{series}/{index}"

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

            if computed:
                value = zscore_of_latest(await self._fetch_series(series, index))
            else:
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

    async def _fetch_series(self, series: str, index: str) -> list[Decimal]:
        """Full series values, leading/embedded nulls dropped.

        The full-series payload is ~55KB (day1 since 2009) vs a scalar
        `latest` — still one cached call per TTL, but give it a more
        generous timeout than the scalar fetch.
        """
        url = f"{self.base_url}/api/series/{series}/{index}"
        try:
            async with httpx.AsyncClient(
                timeout=max(self.timeout_s, 15.0),
                headers={"User-Agent": _UA, "Accept": "application/json"},
            ) as client:
                resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json().get("data")
            if not isinstance(data, list):
                raise ValueError("BRK series response has no 'data' list")
            return [Decimal(str(v)) for v in data if v is not None]
        except (httpx.HTTPError, ValueError, ArithmeticError) as e:
            logger.warning("BRK series %s/%s fetch failed: %s", series, index, e)
            raise OnchainSignalError(
                f"BRK series fetch failed for {series}/{index}: {e}"
            ) from e


_default_client: Optional[OnchainClient] = None


def get_default_client() -> OnchainClient:
    global _default_client
    if _default_client is None:
        _default_client = OnchainClient()
    return _default_client
