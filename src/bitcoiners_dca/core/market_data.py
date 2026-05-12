"""
Market-data provider — feeds context to strategy overlays.

Overlays declare what they need via `OverlayContext`:
  - `realized_vol_30d_pct`  for volatility-weighted DCA
  - `price_ath_aed`         for drawdown-aware sizing
  - `price_7d_ago_aed`      for buy-the-dip
  - `hourly_spread_history` for time-of-day scale-by-spread mode

`MarketDataProvider` fetches all of these once per cycle and builds the
context dict that `DCAStrategy.execute(market_context=...)` consumes.

Data sources:
  - CoinGecko free tier — daily price history for vol + dip + ATH
  - The local SQLite trades + cycle_log tables for hourly spread history
    (we record exchange tickers on each cycle; over time the bot
    accumulates its own per-hour spread distribution)

All fetches are cached. Vol + ATH change slowly; we refresh every 6 hours.
Hourly spread history rolls forward over time.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from bitcoiners_dca.core.historical_prices import (
    HistoricalPricesError,
    HistoricalPriceSource,
)
from bitcoiners_dca.persistence.db import Database

logger = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    """Everything overlays might want, computed once per cycle."""
    price_7d_ago_aed: Optional[Decimal] = None
    price_30d_ago_aed: Optional[Decimal] = None
    price_ath_aed: Optional[Decimal] = None
    realized_vol_30d_pct: Optional[Decimal] = None
    hourly_spread_history: Optional[dict[int, Decimal]] = None

    def to_context_dict(self) -> dict:
        return {
            "price_7d_ago_aed": self.price_7d_ago_aed,
            "price_30d_ago_aed": self.price_30d_ago_aed,
            "price_ath_aed": self.price_ath_aed,
            "realized_vol_30d_pct": self.realized_vol_30d_pct,
            "hourly_spread_history": self.hourly_spread_history,
        }


class MarketDataProvider:
    """Builds a MarketSnapshot per cycle from CoinGecko + the local DB.

    Args:
        db: SQLite database (for hourly spread history).
        vs_currency: quote currency for the price feed (default "aed").
        history_source: injectable for tests. Default: real CoinGecko.
    """

    def __init__(
        self,
        db: Database,
        vs_currency: str = "aed",
        history_source: Optional[HistoricalPriceSource] = None,
    ):
        self.db = db
        self.vs_currency = vs_currency
        self.history_source = history_source or HistoricalPriceSource()
        self._cached_snapshot: Optional[MarketSnapshot] = None
        self._cache_until: Optional[datetime] = None

    def snapshot(self, now: Optional[datetime] = None) -> MarketSnapshot:
        """Return a cached snapshot; refresh every 6 hours.

        `now` is injectable for tests. Production callers leave it None
        (defaults to wall-clock).
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if (
            self._cached_snapshot is not None
            and self._cache_until is not None
            and now < self._cache_until
        ):
            return self._cached_snapshot

        snap = self._build_snapshot(now)
        self._cached_snapshot = snap
        self._cache_until = now + timedelta(hours=6)
        return snap

    def _build_snapshot(self, now: datetime) -> MarketSnapshot:
        snap = MarketSnapshot()

        # Price history (covers 7d-ago, 30d-ago, ATH, realized vol)
        try:
            points = self.history_source.fetch(
                vs_currency=self.vs_currency, days=365
            )
        except HistoricalPricesError as e:
            logger.warning("Price history unavailable for market snapshot: %s", e)
            points = []

        if points:
            # ATH over the available window (1 year)
            snap.price_ath_aed = max(p.price for p in points)

            # Find prices closest to 7 and 30 days ago
            seven_days_ago = (now - timedelta(days=7)).date()
            thirty_days_ago = (now - timedelta(days=30)).date()

            def _nearest_price(target_day):
                # Walk backwards up to ±3 days to find the closest data point
                for offset in range(0, 4):
                    for d in (
                        target_day - timedelta(days=offset),
                        target_day + timedelta(days=offset),
                    ):
                        match = next((p for p in points if p.day == d), None)
                        if match:
                            return match.price
                return None

            snap.price_7d_ago_aed = _nearest_price(seven_days_ago)
            snap.price_30d_ago_aed = _nearest_price(thirty_days_ago)

            # 30-day realized volatility (annualized, std-dev of daily log-returns × √365)
            snap.realized_vol_30d_pct = self._realized_vol_30d(points, now)

        # Hourly spread history from the local cycle_log (when we have it)
        snap.hourly_spread_history = self._hourly_spread_history()

        return snap

    @staticmethod
    def _realized_vol_30d(points, now: datetime) -> Optional[Decimal]:
        """Standard 30d realized vol formula. Returns None if too little data."""
        cutoff = (now - timedelta(days=30)).date()
        recent = [p for p in points if p.day >= cutoff]
        if len(recent) < 5:
            return None
        # Compute daily log-returns
        log_returns: list[float] = []
        for prev, nxt in zip(recent[:-1], recent[1:]):
            if prev.price <= 0 or nxt.price <= 0:
                continue
            try:
                log_returns.append(math.log(float(nxt.price) / float(prev.price)))
            except ValueError:
                continue
        if len(log_returns) < 3:
            return None
        mean = sum(log_returns) / len(log_returns)
        var = sum((r - mean) ** 2 for r in log_returns) / max(len(log_returns) - 1, 1)
        daily_std = math.sqrt(var)
        annualized_pct = daily_std * math.sqrt(365) * 100
        return Decimal(str(round(annualized_pct, 2)))

    def _hourly_spread_history(self) -> Optional[dict[int, Decimal]]:
        """Median spread% per hour-of-day from past cycle observations.

        We piggyback on the `trades` table — every recorded buy has a
        timestamp and an effective price; we DON'T currently log
        bid/ask separately. So for now this returns None (the time-of-day
        overlay in scale_by_spread mode is a no-op until we log spreads
        explicitly).
        """
        # TODO v0.6: log ticker.spread_pct on each cycle into a new
        # ticker_observations table, then aggregate here.
        return None
