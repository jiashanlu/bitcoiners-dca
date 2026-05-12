"""
Funding-rate monitor — detect when BTC perpetual funding crosses configurable
thresholds and alert the user via the existing notifier.

Why it matters: when funding rates spike high-positive (e.g. ≥15% annualized),
a classic basis trade — long spot + short perp, fully hedged — becomes a real
positive-carry opportunity. Bull-market mania periods (peaks of 2021, parts of
2024) saw funding sustain +30 to +50% APY. The bot does NOT auto-execute the
basis trade; it just tells you when the math becomes attractive so you can
decide. See `docs/FUNDING_MONITOR.md` for context.

Sources we support today:
  - OKX (BTC-USDT-SWAP, public endpoint, no auth)

Cooldown state lives in the meta table so restarts don't re-spam alerts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx

from bitcoiners_dca.persistence.db import Database

logger = logging.getLogger(__name__)


# OKX funding settles every 8 hours → 1095 settlements per year.
_OKX_FUNDINGS_PER_YEAR = Decimal(3 * 365)


@dataclass(frozen=True)
class FundingReading:
    instrument: str
    exchange: str
    rate_per_period: Decimal     # raw funding rate (e.g. 0.0001 = 0.01% per 8h)
    annualized_pct: Decimal      # rate × periods/year × 100
    settles_at: datetime         # next settlement time


def _meta_key(exchange: str, instrument: str, suffix: str) -> str:
    return f"funding_monitor.{exchange}.{instrument}.{suffix}"


class FundingMonitor:
    """Polls a funding-rate source and emits alerts when thresholds are crossed."""

    def __init__(
        self,
        db: Database,
        alert_threshold_pct: Decimal = Decimal("15.0"),
        alert_negative_threshold_pct: Decimal = Decimal("-10.0"),
        alert_cooldown_hours: int = 24,
        instruments: Optional[list[dict]] = None,
        http_timeout_seconds: float = 15.0,
    ):
        self.db = db
        self.alert_threshold_pct = alert_threshold_pct
        self.alert_negative_threshold_pct = alert_negative_threshold_pct
        self.cooldown = timedelta(hours=alert_cooldown_hours)
        self.instruments = instruments or [
            {"exchange": "okx", "symbol": "BTC-USDT-SWAP"}
        ]
        self._timeout = http_timeout_seconds

    async def poll(self) -> list[FundingReading]:
        """Fetch all configured instruments. Returns the readings."""
        out: list[FundingReading] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for inst in self.instruments:
                ex = inst["exchange"].lower()
                sym = inst["symbol"]
                try:
                    if ex == "okx":
                        reading = await self._fetch_okx(client, sym)
                        out.append(reading)
                    else:
                        logger.warning("Unsupported funding source: %s", ex)
                except Exception as e:
                    logger.warning("Funding fetch failed for %s/%s: %s", ex, sym, e)
        return out

    async def _fetch_okx(self, client: httpx.AsyncClient, symbol: str) -> FundingReading:
        url = f"https://www.okx.com/api/v5/public/funding-rate?instId={symbol}"
        resp = await client.get(url, headers={"User-Agent": "bitcoiners-dca"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX funding API: {data.get('msg')}")
        d = data["data"][0]
        rate = Decimal(d["fundingRate"])
        annualized = rate * _OKX_FUNDINGS_PER_YEAR * Decimal(100)
        settles = datetime.fromtimestamp(int(d["nextFundingTime"]) / 1000, tz=timezone.utc)
        return FundingReading(
            instrument=symbol,
            exchange="okx",
            rate_per_period=rate,
            annualized_pct=annualized,
            settles_at=settles,
        )

    def evaluate_alert(self, reading: FundingReading) -> Optional[str]:
        """Decide whether `reading` warrants an alert. Returns the alert
        message, or None if no alert should fire (below threshold or in
        cooldown).
        """
        ann = reading.annualized_pct
        if (
            ann >= self.alert_threshold_pct
            or ann <= self.alert_negative_threshold_pct
        ):
            if self._in_cooldown(reading):
                return None
            self._set_last_alert(reading)
            direction = "longs paying shorts" if ann > 0 else "shorts paying longs"
            return (
                f"Funding spike on {reading.exchange.upper()} {reading.instrument}: "
                f"{ann:+.2f}% annualized ({direction}). "
                f"Next settle at {reading.settles_at.isoformat()}."
            )
        return None

    # --- cooldown state ---

    def _last_alert(self, reading: FundingReading) -> Optional[datetime]:
        raw = self.db.get_meta(_meta_key(reading.exchange, reading.instrument, "last_alert"))
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _in_cooldown(self, reading: FundingReading) -> bool:
        last = self._last_alert(reading)
        if last is None:
            return False
        return datetime.now(timezone.utc) - last < self.cooldown

    def _set_last_alert(self, reading: FundingReading) -> None:
        self.db.set_meta(
            _meta_key(reading.exchange, reading.instrument, "last_alert"),
            datetime.now(timezone.utc).isoformat(),
        )
