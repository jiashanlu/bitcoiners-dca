"""
Scheduler — long-running daemon that runs the DCA cycle on a cron schedule
plus polls for arbitrage opportunities at a configurable interval.

Built on apscheduler.AsyncIOScheduler.

Job structure:
  - dca_cycle    : runs at user-configured time (cron expression)
  - arbitrage    : runs every N seconds (poll interval from config)
  - health_check : every 5 minutes, validates exchange connectivity

The daemon survives transient errors. Each job logs to the cycle_log table
in SQLite so you can audit what happened, when, and why.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bitcoiners_dca.core.arbitrage import ArbitrageMonitor
from bitcoiners_dca.core.funding_monitor import FundingMonitor
from bitcoiners_dca.core.market_data import MarketDataProvider
from bitcoiners_dca.core.notifications import Notifier
from bitcoiners_dca.core.risk import RiskManager
from bitcoiners_dca.core.router import SmartRouter
from bitcoiners_dca.core.strategy import DCAStrategy, StrategyConfig
from bitcoiners_dca.exchanges.base import Exchange, ExchangeError
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.utils.config import AppConfig

logger = logging.getLogger(__name__)


# === DAY-OF-WEEK MAPPING ===

_DOW_MAP = {
    "mon": "mon", "monday": "mon",
    "tue": "tue", "tuesday": "tue",
    "wed": "wed", "wednesday": "wed",
    "thu": "thu", "thursday": "thu",
    "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat",
    "sun": "sun", "sunday": "sun",
}


def _build_cron_trigger(cfg: AppConfig) -> CronTrigger:
    """Translate the YAML config into an apscheduler CronTrigger.

    Supports:
      frequency: hourly  -> every N hours at :MM (cfg.strategy.every_n_hours)
      frequency: daily   -> every day at HH:MM
      frequency: weekly  -> day_of_week at HH:MM
      frequency: monthly -> 1st of the month at HH:MM

    Hourly with every_n_hours=1 fires 24x/day; every_n_hours=2 fires 12x/day,
    etc. Per-cycle base amount is unchanged, so the risk caps
    (max_daily_aed + max_single_buy_aed) protect against over-spend. Set
    those before flipping to aggressive cadences.
    """
    freq = cfg.strategy.frequency.lower()
    hour, minute = cfg.strategy.time.split(":")
    tz = cfg.strategy.timezone

    kwargs: dict = {"minute": int(minute), "timezone": tz}

    if freq == "hourly":
        n = max(1, int(getattr(cfg.strategy, "every_n_hours", 1) or 1))
        if n == 1:
            # Every hour at the configured minute — `hour` wildcard.
            pass
        elif 24 % n == 0:
            # 2,3,4,6,8,12 → clean repeating cron (e.g. */2 = 0,2,4,…22)
            kwargs["hour"] = f"*/{n}"
        else:
            # Non-divisors of 24 (5, 7, 9, 10, 11, …) would drift across
            # the day boundary if we used */N. Snap to the closest clean
            # divisor below and warn — keeps cron deterministic.
            cleaner = max(d for d in (1, 2, 3, 4, 6, 8, 12) if d <= n)
            logger.warning(
                "every_n_hours=%d isn't a clean divisor of 24; using every %d hours instead. "
                "Stick to 1, 2, 3, 4, 6, 8, or 12 for predictable cadence.",
                n, cleaner,
            )
            kwargs["hour"] = f"*/{cleaner}" if cleaner > 1 else "*"
    elif freq == "daily":
        kwargs["hour"] = int(hour)
    elif freq == "weekly":
        kwargs["hour"] = int(hour)
        dow = _DOW_MAP.get(cfg.strategy.day_of_week.lower())
        if not dow:
            raise ValueError(f"Invalid day_of_week: {cfg.strategy.day_of_week}")
        kwargs["day_of_week"] = dow
    elif freq == "monthly":
        kwargs["hour"] = int(hour)
        kwargs["day"] = 1
    else:
        raise ValueError(f"Invalid frequency: {freq}")

    return CronTrigger(**kwargs)


# === SCHEDULER ===

class DCAScheduler:
    """Wires together cron + arbitrage polling + health checks.

    Hot config reload: when `rebuild_dependencies` is provided, the scheduler
    will call it at the start of each scheduled task to pick up dashboard-
    initiated config changes without a daemon restart. The callable returns a
    fresh (config, exchanges, strategy, router, monitor, risk) tuple.
    """

    def __init__(
        self,
        config: AppConfig,
        exchanges: list[Exchange],
        strategy: DCAStrategy,
        router: SmartRouter,
        monitor: ArbitrageMonitor,
        db: Database,
        notifier: Notifier,
        risk: Optional[RiskManager] = None,
        rebuild_dependencies=None,
    ):
        self.config = config
        self.exchanges = exchanges
        self.strategy = strategy
        self.router = router
        self.monitor = monitor
        self.db = db
        self.notifier = notifier
        self.risk = risk or RiskManager(
            db=db,
            max_daily_aed=config.risk.max_daily_aed,
            max_single_buy_aed=config.risk.max_single_buy_aed,
            max_consecutive_failures=config.risk.max_consecutive_failures,
        )
        self._rebuild_dependencies = rebuild_dependencies
        self.market_data = MarketDataProvider(db=db)
        self.funding_monitor: Optional[FundingMonitor] = None
        if config.funding_monitor.enabled:
            self.funding_monitor = FundingMonitor(
                db=db,
                alert_threshold_pct=config.funding_monitor.alert_threshold_pct,
                alert_negative_threshold_pct=config.funding_monitor.alert_negative_threshold_pct,
                alert_cooldown_hours=config.funding_monitor.alert_cooldown_hours,
                instruments=[
                    {"exchange": i.exchange, "symbol": i.symbol}
                    for i in config.funding_monitor.instruments
                ],
            )
        self._scheduler = AsyncIOScheduler()
        self._stop_event = asyncio.Event()

    def _historical_price_7d_ago(self) -> Optional[Decimal]:
        """Convenience for the legacy path. Reads from MarketDataProvider."""
        return self.market_data.snapshot().price_7d_ago_aed

    async def _reload_if_changed(self) -> None:
        """Refresh dependencies from disk if config.yaml changed.

        Cheap to call at the top of every scheduled task. If the rebuild
        factory wasn't supplied at construction (e.g. legacy callers), this
        is a no-op.
        """
        if self._rebuild_dependencies is None:
            return
        try:
            fresh = self._rebuild_dependencies()
        except Exception as e:
            logger.warning("Config reload failed; keeping in-memory state: %s", e)
            return
        # Replace mutable references — old exchange clients leak HTTP sessions,
        # but the close-loop runs on `shutdown()`. For a hot-reload we accept
        # the leak: next cycle uses fresh clients.
        old_exchanges = self.exchanges
        self.config = fresh["config"]
        self.exchanges = fresh["exchanges"]
        self.strategy = fresh["strategy"]
        self.router = fresh["router"]
        self.monitor = fresh["monitor"]
        self.risk = fresh.get("risk", self.risk)
        # Best-effort close of replaced clients
        for ex in old_exchanges:
            if ex not in self.exchanges:
                try:
                    await ex.close()
                except Exception:
                    pass

    async def _run_dca_cycle(self) -> None:
        """One scheduled DCA cycle. Errors are caught + logged + notified
        so the scheduler keeps running."""
        await self._reload_if_changed()

        # Risk pre-check — paused state + daily/single-buy caps.
        decision = self.risk.evaluate(self.config.strategy.amount_aed)
        if not decision.allow:
            logger.warning(
                "DCA cycle skipped by risk manager: %s", "; ".join(decision.reasons)
            )
            await self.notifier.notify_error(
                "DCA cycle skipped (risk)", "; ".join(decision.reasons)
            )
            return

        try:
            logger.info(
                "Starting DCA cycle (risk-approved amount=AED %s)", decision.amount_aed
            )
            snap = self.market_data.snapshot()
            result = await self.strategy.execute(
                self.exchanges,
                historical_price_7d_ago=snap.price_7d_ago_aed,
                risk_cap_aed=decision.amount_aed,
                market_context=snap.to_context_dict(),
            )
            if decision.reasons:
                result.notes.extend(decision.reasons)
            self.db.record_cycle(result)
            await self.notifier.notify_cycle(result)
            success = bool(result.order) and not result.errors
            self.risk.record_cycle_result(success=success)
            logger.info(
                "DCA cycle complete: order=%s errors=%d",
                result.order.order_id if result.order else "none",
                len(result.errors),
            )
        except Exception as e:
            logger.exception("DCA cycle failed unexpectedly")
            self.risk.record_cycle_result(success=False)
            # Persist the failure so the dashboard can show it. Empty
            # cycle_log made the bot look silently broken — a customer
            # has no way to know cycles even ran. Record a synthetic
            # CycleResult with the error string.
            try:
                from datetime import datetime, timezone
                from bitcoiners_dca.core.strategy import ExecutionResult
                fail_result = ExecutionResult(
                    timestamp=datetime.now(timezone.utc),
                    intended_amount_aed=decision.amount_aed,
                    overlay_applied=None,
                    routing_decision=None,
                    errors=[str(e)[:500]],
                )
                self.db.record_cycle(fail_result)
            except Exception:
                logger.exception("failed to record cycle failure")
            await self.notifier.notify_error("DCA cycle failed", str(e))

    async def _run_arbitrage_check(self) -> None:
        """Poll for arbitrage. Alerts only on net-positive opportunities."""
        await self._reload_if_changed()
        try:
            if len(self.exchanges) < 2:
                return  # need at least 2 exchanges
            opps = await self.monitor.detect(self.exchanges)
            for opp in opps:
                self.db.record_arbitrage(opp, alerted=True)
                await self.notifier.notify_arbitrage(opp)
            if opps:
                logger.info("Found %d arbitrage opportunities", len(opps))
        except Exception as e:
            logger.exception("Arbitrage check failed: %s", e)

    async def _run_funding_check(self) -> None:
        if not self.funding_monitor:
            return
        try:
            readings = await self.funding_monitor.poll()
            for r in readings:
                msg = self.funding_monitor.evaluate_alert(r)
                if msg:
                    logger.info("Funding alert: %s", msg)
                    await self.notifier.notify_error("Funding-rate alert", msg)
        except Exception as e:
            logger.warning("Funding monitor poll failed: %s", e)

    async def _run_health_check(self) -> None:
        """Verify each exchange is reachable + authenticated."""
        for ex in self.exchanges:
            try:
                await ex.health_check()
            except Exception as e:
                logger.error("Health check FAILED for %s: %s", ex.name, e)
                await self.notifier.notify_error(
                    f"{ex.name} health check failed", str(e)
                )

    def _install_jobs(self) -> None:
        # DCA cycle on cron schedule
        self._scheduler.add_job(
            self._run_dca_cycle,
            trigger=_build_cron_trigger(self.config),
            id="dca_cycle",
            replace_existing=True,
            misfire_grace_time=600,  # 10 min — if missed, run when we next can
        )
        logger.info(
            "DCA cycle scheduled: %s %s %s (timezone=%s)",
            self.config.strategy.frequency,
            self.config.strategy.day_of_week,
            self.config.strategy.time,
            self.config.strategy.timezone,
        )

        # Arbitrage polling
        if self.config.arbitrage.enabled and len(self.exchanges) >= 2:
            self._scheduler.add_job(
                self._run_arbitrage_check,
                trigger=IntervalTrigger(
                    seconds=self.config.arbitrage.poll_interval_seconds
                ),
                id="arbitrage",
                replace_existing=True,
            )
            logger.info(
                "Arbitrage polling every %ds",
                self.config.arbitrage.poll_interval_seconds,
            )

        # Health check every 5 minutes
        self._scheduler.add_job(
            self._run_health_check,
            trigger=IntervalTrigger(minutes=5),
            id="health_check",
            replace_existing=True,
        )

        # Funding monitor — opt-in
        if self.funding_monitor:
            self._scheduler.add_job(
                self._run_funding_check,
                trigger=IntervalTrigger(
                    seconds=self.config.funding_monitor.poll_interval_seconds
                ),
                id="funding_monitor",
                replace_existing=True,
            )
            logger.info(
                "Funding monitor enabled (%d instruments, poll every %ds)",
                len(self.config.funding_monitor.instruments),
                self.config.funding_monitor.poll_interval_seconds,
            )

    async def run_forever(self) -> None:
        """Start the scheduler and block until SIGTERM/SIGINT.

        Performs initial health check on startup so we fail fast on bad creds.
        """
        # Initial health check
        await self._run_health_check()

        self._install_jobs()
        self._scheduler.start()

        # Wire SIGTERM/SIGINT to clean shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows / non-Unix
                pass

        logger.info("Scheduler running. Press Ctrl+C to stop.")

        try:
            await self._stop_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutting down scheduler...")
        self._scheduler.shutdown(wait=False)
        for ex in self.exchanges:
            try:
                await ex.close()
            except Exception:
                pass
        self.db.close()
        logger.info("Shutdown complete.")
