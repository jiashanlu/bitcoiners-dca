"""
Historical-price fetcher — CoinGecko-backed, file-cached.

CoinGecko's free tier serves up to 365 days of daily-granularity BTC/AED data
at /coins/bitcoin/market_chart. That's enough for a typical UAE Bitcoiner DCA
backtest (weekly/monthly cadence over a year). Longer ranges error out with a
clear message pointing at a paid tier or a different data source.

The fetch is cached on disk to avoid hammering the public API across repeated
backtest runs — caches by (currency, days) and invalidates after 1 hour for
the "today" window.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEFAULT_CACHE_DIR = Path.home() / ".bitcoiners-dca-cache"
CACHE_TTL_SECONDS = 3600  # 1 hour
COINGECKO_FREE_TIER_MAX_DAYS = 365


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    price: Decimal

    @property
    def day(self) -> date:
        return self.timestamp.date()


class HistoricalPricesError(Exception):
    pass


class HistoricalPriceSource:
    """Fetches and caches daily BTC/<quote> price history from CoinGecko."""

    def __init__(
        self,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        timeout_seconds: float = 30.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout_seconds

    def _cache_path(self, vs_currency: str, days: int) -> Path:
        return self.cache_dir / f"btc-{vs_currency.lower()}-{days}d.json"

    def _read_cache(self, path: Path) -> Optional[list[PricePoint]]:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            if time.time() - payload["fetched_at"] > CACHE_TTL_SECONDS:
                return None
            return [
                PricePoint(
                    timestamp=datetime.fromisoformat(p["ts"]),
                    price=Decimal(p["price"]),
                )
                for p in payload["points"]
            ]
        except (ValueError, KeyError) as e:
            logger.warning("Stale cache at %s: %s", path, e)
            return None

    def _write_cache(self, path: Path, points: list[PricePoint]) -> None:
        path.write_text(json.dumps({
            "fetched_at": time.time(),
            "points": [
                {"ts": p.timestamp.isoformat(), "price": str(p.price)}
                for p in points
            ],
        }))

    def fetch(
        self,
        vs_currency: str = "aed",
        days: int = 365,
    ) -> list[PricePoint]:
        """Daily price points, ordered oldest → newest."""
        if days > COINGECKO_FREE_TIER_MAX_DAYS:
            raise HistoricalPricesError(
                f"CoinGecko free tier maxes at {COINGECKO_FREE_TIER_MAX_DAYS} "
                f"daily points (requested {days}). Use --from no earlier than "
                f"{COINGECKO_FREE_TIER_MAX_DAYS} days ago, or wire a paid "
                f"market-data source."
            )

        cache_path = self._cache_path(vs_currency, days)
        cached = self._read_cache(cache_path)
        if cached is not None:
            logger.info("Using cached price history (%d points)", len(cached))
            return cached

        url = f"{COINGECKO_BASE}/coins/bitcoin/market_chart"
        params = {"vs_currency": vs_currency, "days": days}
        logger.info("Fetching BTC/%s history (%dd) from CoinGecko", vs_currency.upper(), days)
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            raise HistoricalPricesError(
                f"CoinGecko fetch failed: {e}"
            ) from e

        prices = data.get("prices", [])
        if not prices:
            raise HistoricalPricesError(
                f"CoinGecko returned no prices: {data}"
            )

        points = [
            PricePoint(
                timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                price=Decimal(str(round(price, 4))),
            )
            for ts_ms, price in prices
        ]
        self._write_cache(cache_path, points)
        return points

    def slice_range(
        self,
        points: list[PricePoint],
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> list[PricePoint]:
        out = points
        if from_date:
            out = [p for p in out if p.day >= from_date]
        if to_date:
            out = [p for p in out if p.day <= to_date]
        return out
