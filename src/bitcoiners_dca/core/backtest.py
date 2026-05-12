"""
Backtest engine — replays a DCA strategy over historical price data.

Pure functions, no I/O. Feed it a `BacktestConfig` plus a sequence of
`PricePoint`s and it returns a `BacktestResult` with every simulated cycle
plus a summary (total AED spent, total BTC accumulated, avg cost, dip-overlay
triggers, comparison vs. naive flat DCA).

Intentionally simple: market-buy at the day's close price minus a fixed taker
fee. No slippage model, no exchange-specific spread — for a long-horizon DCA
this is plenty accurate (the bot's smart-routing edge is bps, dwarfed by
multi-month BTC moves).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from bitcoiners_dca.core.historical_prices import PricePoint


@dataclass(frozen=True)
class BacktestConfig:
    base_amount_aed: Decimal
    frequency: str = "weekly"            # "daily" | "weekly" | "monthly"
    day_of_week: int = 0                 # 0=Monday, used when frequency=weekly
    taker_fee_pct: Decimal = Decimal("0.005")   # default to BitOasis Pro retail
    dip_overlay_enabled: bool = False
    dip_threshold_pct: Decimal = Decimal("-10")  # if 7d price down ≥ this, trigger
    dip_lookback_days: int = 7
    dip_multiplier: Decimal = Decimal("2.0")


@dataclass
class BacktestCycle:
    day: date
    price_aed: Decimal
    aed_spent: Decimal
    btc_bought: Decimal
    overlay_applied: bool


@dataclass
class BacktestResult:
    config: BacktestConfig
    cycles: list[BacktestCycle] = field(default_factory=list)
    start_day: Optional[date] = None
    end_day: Optional[date] = None

    @property
    def total_aed_spent(self) -> Decimal:
        return sum((c.aed_spent for c in self.cycles), Decimal(0))

    @property
    def total_btc_bought(self) -> Decimal:
        return sum((c.btc_bought for c in self.cycles), Decimal(0))

    @property
    def avg_price_aed(self) -> Decimal:
        if self.total_btc_bought == 0:
            return Decimal(0)
        return self.total_aed_spent / self.total_btc_bought

    @property
    def overlay_triggers(self) -> int:
        return sum(1 for c in self.cycles if c.overlay_applied)

    @property
    def cycle_count(self) -> int:
        return len(self.cycles)


def _should_fire(day: date, cfg: BacktestConfig) -> bool:
    if cfg.frequency == "daily":
        return True
    if cfg.frequency == "weekly":
        return day.weekday() == cfg.day_of_week
    if cfg.frequency == "monthly":
        return day.day == 1
    raise ValueError(f"Unknown frequency: {cfg.frequency}")


def _price_lookup(points: list[PricePoint]) -> dict[date, Decimal]:
    """Last price seen for each calendar day (CoinGecko sometimes returns
    multiple intraday samples on the most recent day)."""
    out: dict[date, Decimal] = {}
    for p in points:
        out[p.day] = p.price
    return out


def run_backtest(
    cfg: BacktestConfig,
    points: list[PricePoint],
) -> BacktestResult:
    """Replay the DCA strategy across the supplied price history."""
    if not points:
        return BacktestResult(config=cfg)

    by_day = _price_lookup(points)
    days_sorted = sorted(by_day.keys())
    start_day, end_day = days_sorted[0], days_sorted[-1]

    cycles: list[BacktestCycle] = []
    current = start_day
    while current <= end_day:
        if _should_fire(current, cfg) and current in by_day:
            price = by_day[current]
            amount = cfg.base_amount_aed
            overlay = False

            if cfg.dip_overlay_enabled:
                lookback_day = current - timedelta(days=cfg.dip_lookback_days)
                ref_price = _last_price_on_or_before(by_day, lookback_day)
                if ref_price and ref_price > 0:
                    pct_change = ((price - ref_price) / ref_price) * Decimal(100)
                    if pct_change <= cfg.dip_threshold_pct:
                        amount = cfg.base_amount_aed * cfg.dip_multiplier
                        overlay = True

            effective_price = price * (Decimal(1) + cfg.taker_fee_pct)
            btc = amount / effective_price
            cycles.append(BacktestCycle(
                day=current,
                price_aed=price,
                aed_spent=amount,
                btc_bought=btc,
                overlay_applied=overlay,
            ))
        current += timedelta(days=1)

    return BacktestResult(
        config=cfg,
        cycles=cycles,
        start_day=start_day,
        end_day=end_day,
    )


def _last_price_on_or_before(
    by_day: dict[date, Decimal], target: date
) -> Optional[Decimal]:
    """Find the most recent price at or before `target`. Walks back ≤7 days."""
    for offset in range(0, 8):
        d = target - timedelta(days=offset)
        if d in by_day:
            return by_day[d]
    return None


def naive_baseline(cfg: BacktestConfig, points: list[PricePoint]) -> BacktestResult:
    """Run the same cadence but WITHOUT the dip overlay — to quantify what the
    overlay actually added."""
    flat = BacktestConfig(
        base_amount_aed=cfg.base_amount_aed,
        frequency=cfg.frequency,
        day_of_week=cfg.day_of_week,
        taker_fee_pct=cfg.taker_fee_pct,
        dip_overlay_enabled=False,
    )
    return run_backtest(flat, points)
