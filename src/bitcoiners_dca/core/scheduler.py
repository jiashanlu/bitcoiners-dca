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


# Consecutive failed health checks an exchange must rack up before we page the
# operator. Health checks run every 5 min; at 2, an exchange has to be down for
# ~10 min before anyone hears about it. This is the second line of defence
# behind each adapter's own transient-error retry — together they keep a brief
# Cloudflare 502 / network blip from generating a spurious alert while still
# surfacing a genuine sustained outage.
HEALTH_ALERT_THRESHOLD = 2


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
        # snap_every_n_hours is the SINGLE source of truth shared with
        # derive_per_cycle — before this, the cron snapped a non-divisor
        # (e.g. 5 → fires every 4h) while the per-cycle amount stayed sized
        # for the raw 5h cadence, overspending the user's budget ~25% on
        # every cycle (audit 2026-06-10 P1).
        from bitcoiners_dca.core.strategy import snap_every_n_hours
        raw = getattr(cfg.strategy, "every_n_hours", 1)
        n = snap_every_n_hours(raw)
        if n != max(1, int(raw or 1)):
            logger.warning(
                "every_n_hours=%s isn't a clean divisor of 24; using every %d "
                "hours instead (and sizing the per-cycle amount to match). "
                "Stick to 1, 2, 3, 4, 6, 8, 12, or 24 for predictable cadence.",
                raw, n,
            )
        if n > 1:
            kwargs["hour"] = f"*/{n}"
        # n == 1: every hour at the configured minute — `hour` wildcard.
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
            timezone_str=config.strategy.timezone or "Asia/Dubai",
        )
        # Page the operator's admin Telegram on auto-pause transition.
        # The notify hook is a no-op when ADMIN_TG_* env vars are unset
        # (e.g. self-host with no central monitoring), so this is safe
        # to wire unconditionally.
        from bitcoiners_dca.core.notifications import send_admin_alert
        self.risk.on_auto_pause = lambda reason: send_admin_alert(
            f"Tenant auto-paused: {reason}\n\n"
            f"Pause clears manually via dashboard /controls/resume "
            f"or CLI `bitcoiners-dca risk resume`. Investigate the "
            f"underlying failure (exchange auth, daily-cap, etc) first.",
            tag="cycle-fail",
        )
        self._rebuild_dependencies = rebuild_dependencies
        self.market_data = MarketDataProvider(db=db)
        self.funding_monitor = self._build_funding_monitor(config)
        self._scheduler = AsyncIOScheduler()
        self._stop_event = asyncio.Event()
        # Serialises the periodic jobs against each other and against the
        # cycle's hot-reload. _cycle_in_progress only protects the CYCLE from
        # the 5-min jobs; the jobs were not protected from each other — the
        # arbitrage job's _reload_if_changed swapped self.exchanges and
        # closed the old clients while a concurrently-running health check
        # was still iterating its snapshot of the old list, producing
        # "client has been closed" failures, false health-streak increments
        # and spurious pages (audit 2026-06-10 P2). Jobs hold this for their
        # whole body; the cycle holds it only across its reload call (the
        # flag covers the rest), so the lock is never held while awaiting
        # itself.
        self._jobs_lock = asyncio.Lock()
        # True for the full duration of a DCA cycle. The 5-minute jobs
        # (arbitrage / health / funding) check this and bail rather than
        # call _reload_if_changed() — a rebuild closes the live exchange
        # clients, and doing that mid-cycle crashes the in-flight request
        # ("'NoneType' has no attribute 'getaddrinfo'" on ccxt/aiodns,
        # "Cannot send a request, as the client has been closed" on httpx).
        # Set synchronously at cycle entry so any later-firing job sees it.
        self._cycle_in_progress = False

        # Per-exchange consecutive health-check failure streak. We only alert
        # once an exchange has failed HEALTH_ALERT_THRESHOLD checks IN A ROW —
        # a single transient blip (e.g. a BitOasis/Cloudflare 502 that outlasts
        # the adapter's retry window) shouldn't page anyone; a real sustained
        # outage should. Reset to 0 on the first success, which also emits a
        # recovery notice if we'd previously alerted.
        self._health_fail_streak: dict[str, int] = {}

    def _build_funding_monitor(self, config: AppConfig) -> Optional[FundingMonitor]:
        if not config.funding_monitor.enabled:
            return None
        return FundingMonitor(
            db=self.db,
            alert_threshold_pct=config.funding_monitor.alert_threshold_pct,
            alert_negative_threshold_pct=config.funding_monitor.alert_negative_threshold_pct,
            alert_cooldown_hours=config.funding_monitor.alert_cooldown_hours,
            instruments=[
                {"exchange": i.exchange, "symbol": i.symbol}
                for i in config.funding_monitor.instruments
            ],
        )

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
        old_cfg = self.config
        self.config = fresh["config"]
        self.exchanges = fresh["exchanges"]
        self.strategy = fresh["strategy"]
        self.router = fresh["router"]
        self.monitor = fresh["monitor"]
        # Notifier is the ALERTING surface of a money bot — a customer who
        # fixes their Telegram chat_id/token via the dashboard reasonably
        # believes it took effect, but the daemon kept the boot-time
        # Notifier and sent (or failed to send) to the old destination
        # until restart (audit 2026-06-10 P2). Stateless → cheap to swap.
        # Key-presence check (not .get default) so legacy rebuild factories
        # without a "notifier" key leave the attribute untouched.
        if "notifier" in fresh:
            self.notifier = fresh["notifier"]
        # FundingMonitor: rebuild on config change, and add/remove/
        # reschedule its job to match — previously enable/threshold edits
        # never applied, and enabling after boot never installed the job.
        old_fm_cfg = getattr(old_cfg, "funding_monitor", None)
        new_fm_cfg = getattr(self.config, "funding_monitor", None)
        if new_fm_cfg is not None and old_fm_cfg != new_fm_cfg:
            self.funding_monitor = self._build_funding_monitor(self.config)
            try:
                job = self._scheduler.get_job("funding_monitor")
                if self.funding_monitor is None:
                    if job is not None:
                        self._scheduler.remove_job("funding_monitor")
                        logger.info("Funding monitor disabled — job removed")
                else:
                    trigger = IntervalTrigger(
                        seconds=self.config.funding_monitor.poll_interval_seconds
                    )
                    if job is None:
                        self._scheduler.add_job(
                            self._run_funding_check, trigger=trigger,
                            id="funding_monitor", replace_existing=True,
                        )
                        logger.info("Funding monitor enabled — job installed")
                    else:
                        self._scheduler.reschedule_job(
                            "funding_monitor", trigger=trigger
                        )
            except Exception as e:
                logger.warning("funding_monitor job update failed: %s", e)
        # Hot-reload the risk CAPS in place on the existing RiskManager rather
        # than swapping the instance. The rebuild factory intentionally returns
        # no "risk" key: a fresh RiskManager would drop the on_auto_pause hook
        # wired in __init__, and a swap isn't needed — all risk STATE (pause
        # flag, consecutive-failure count, daily spend) lives in the DB; only
        # the caps live on the instance. Before this, dashboard edits to
        # risk.max_* silently never took effect until a container restart
        # (fresh.get("risk", ...) always fell back to the stale startup caps,
        # clamping a raised single-buy cap back down to the boot-time value).
        self.risk.max_daily_aed = self.config.risk.max_daily_aed
        self.risk.max_single_buy_aed = self.config.risk.max_single_buy_aed
        self.risk.max_consecutive_failures = self.config.risk.max_consecutive_failures
        self.risk.timezone_str = self.config.strategy.timezone or "Asia/Dubai"
        # If cron-relevant fields changed, swap the trigger on the live job.
        # Before this fix, customers saved Strategy → frequency: hourly +
        # every_n_hours: 2 and the daemon kept firing on the old schedule
        # until container restart.
        cron_keys = ("frequency", "every_n_hours", "day_of_week", "time", "timezone")
        if any(
            getattr(old_cfg.strategy, k, None) != getattr(self.config.strategy, k, None)
            for k in cron_keys
        ):
            try:
                self._scheduler.reschedule_job(
                    "dca_cycle", trigger=_build_cron_trigger(self.config)
                )
                logger.info(
                    "DCA cron rescheduled: %s every_n_hours=%s %s %s",
                    self.config.strategy.frequency,
                    getattr(self.config.strategy, "every_n_hours", 1),
                    self.config.strategy.day_of_week,
                    self.config.strategy.time,
                )
            except Exception as e:
                logger.warning("reschedule_job failed: %s", e)
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
        # Claim the cycle before the first await so concurrent 5-min jobs
        # won't rebuild/close the exchange clients this cycle relies on.
        self._cycle_in_progress = True
        try:
            # Cross-process cycle lock: if the dashboard Buy-Now (separate
            # process) is mid-cycle, skip this scheduled tick rather than
            # race it on the shared daily-cap read (audit 2026-06-02 #12).
            if not self.db.try_acquire_cycle_lock():
                logger.info(
                    "DCA cycle skipped — another cycle (e.g. dashboard "
                    "Buy-Now) holds the cross-process lock"
                )
                return
            try:
                await self._run_dca_cycle_inner()
            finally:
                self.db.release_cycle_lock()
        finally:
            self._cycle_in_progress = False
        # Refresh the "when is the next buy due" marker the startup
        # catch-up reads — apscheduler has already advanced next_run_time
        # past this fire.
        self._persist_next_fire()

    async def _run_dca_cycle_inner(self) -> None:
        # Reload swaps + closes exchange clients — take the jobs lock so a
        # mid-iteration health/arbitrage/funding job can't be left holding
        # dying clients (the _cycle_in_progress flag keeps jobs from
        # STARTING during the cycle, but not from finishing).
        async with self._jobs_lock:
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
            # cap_aed, NOT amount_aed: amount_aed is the risk-approved BASE,
            # and capping overlay output at the base silently neutered every
            # boost multiplier — dip 2x / drawdown 4x / MVRV 1.5x bought
            # exactly the base on every scheduled cycle (audit 2026-06-10).
            # cap_aed is the real ceiling: min(single-buy cap, daily
            # remainder), or None when no caps are configured.
            result = await self.strategy.execute(
                self.exchanges,
                historical_price_7d_ago=snap.price_7d_ago_aed,
                risk_cap_aed=decision.cap_aed,
                market_context=snap.to_context_dict(),
            )
            if decision.reasons:
                result.notes.extend(decision.reasons)
            self.db.record_cycle(result)
            # Differentiate three outcomes for the risk-manager streak:
            #   real success    → reset counter
            #   deliberate skip → leave counter unchanged (overlay said no,
            #                     maker_only timed out, etc.)
            #   real failure    → increment counter
            # Previously every 0-order cycle counted as failure → hourly
            # frequency + time_of_day [9..18] auto-paused after 5 night cycles.
            # This runs BEFORE notify_cycle: a notification FORMATTING crash
            # must not convert a successful, money-spending buy into a
            # recorded failure (failure-streak increment toward auto-pause +
            # duplicate synthetic failed cycle row + false 'cycle failed'
            # alert — audit 2026-06-10 P3).
            if result.order and not result.errors:
                self.risk.record_cycle_result(success=True)
            elif result.deliberate_skip and not result.errors:
                # Skip — don't touch the counter
                pass
            else:
                self.risk.record_cycle_result(success=False)
            try:
                await self.notifier.notify_cycle(result)
            except Exception:
                # Transport errors are already swallowed inside the
                # notifier; this catches FORMATTING crashes. The cycle
                # itself succeeded and is recorded — never let the message
                # template take it down.
                logger.exception(
                    "notify_cycle failed — cycle succeeded and is recorded; "
                    "suppressing notification error"
                )
            # Multi-hop orphan detection: any error mentioning "Orphan"
            # means a hop succeeded then the next failed, leaving funds
            # stuck on the exchange in an intermediate currency. Surface
            # for manual cleanup via the dashboard.
            self._record_orphan_if_any(result)
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
                self._record_orphan_if_any(fail_result)
            except Exception:
                logger.exception("failed to record cycle failure")
            await self.notifier.notify_error("DCA cycle failed", str(e))

    def _record_orphan_if_any(self, result) -> None:
        """Stash a dashboard-visible orphan-funds banner if this cycle
        ended with funds parked in an intermediate currency.

        Two trigger signals (in order of preference):
          1. Explicit `result.orphan_*` fields set by strategy when hop
             K-1 succeeded but hop K failed (preferred — reliable).
          2. Error-string fallback for "orphan" (legacy paths that
             didn't set the explicit fields).

        Cleared from the UI via db.set_meta('multi_hop.orphan_acknowledged_at', ...).
        """
        import json as _json
        from datetime import datetime, timezone
        explicit_orphan = bool(getattr(result, "orphan_amount", None))
        orphan_errors = [e for e in (result.errors or []) if "Orphan" in e or "orphan" in e]
        if not explicit_orphan and not orphan_errors:
            return
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "errors": orphan_errors[:3],
            "notes": (result.notes or [])[:5],
        }
        if explicit_orphan:
            payload["amount"] = str(result.orphan_amount)
            payload["ccy"] = result.orphan_ccy
            payload["exchange"] = result.orphan_exchange
        try:
            self.db.set_meta("multi_hop.last_orphan", _json.dumps(payload))
        except Exception:
            logger.exception("failed to persist orphan meta")

    async def _run_arbitrage_check(self) -> None:
        """Poll for arbitrage. Alerts only on net-positive opportunities."""
        if self._cycle_in_progress:
            return  # don't rebuild/close clients out from under a live DCA cycle
        async with self._jobs_lock:
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
        if self._cycle_in_progress:
            return  # a cycle is mid-flight — defer to the next 5-min tick
        if not self.funding_monitor:
            return
        async with self._jobs_lock:
            try:
                readings = await self._funding_readings()
                for r in readings:
                    msg = self.funding_monitor.evaluate_alert(r)
                    if msg:
                        logger.info("Funding alert: %s", msg)
                        await self.notifier.notify_error("Funding-rate alert", msg)
            except Exception as e:
                logger.warning("Funding monitor poll failed: %s", e)

    async def _funding_readings(self):
        """Try the hosted Pro API first (one central poll across all
        tenants). Fall back to a direct OKX poll on any failure — the
        bot's local monitor was the original source of truth and
        remains the canonical fallback. Returns a list of
        FundingReading."""
        from decimal import Decimal
        from datetime import datetime
        from bitcoiners_dca.core.funding_monitor import FundingReading
        from bitcoiners_dca.core.pro_api_client import remote_funding_readings

        license_token = getattr(
            getattr(self.config, "license", None), "key", None,
        )
        # Match the bot's configured instrument list — call the server for
        # each. The server currently only supports BTC-USDT-SWAP, so other
        # instruments naturally fall back to local. Cheap, no contention.
        out = []
        for inst in self.funding_monitor.instruments:
            ex = inst["exchange"].lower()
            sym = inst["symbol"]
            remote = await remote_funding_readings(
                license_token, exchange=ex, instrument=sym, hours=1,
            )
            if remote and remote[0]:
                top = remote[0]
                out.append(FundingReading(
                    instrument=top["instrument"],
                    exchange=top["exchange"],
                    rate_per_period=Decimal(str(top["rate_per_period"])),
                    annualized_pct=Decimal(str(top["annualized_pct"])),
                    settles_at=datetime.fromisoformat(top["settles_at"].replace("Z", "+00:00")),
                ))
                continue
            # Fall back to local poll for this instrument.
            local = await self.funding_monitor.poll()
            for r in local:
                if r.instrument == sym and r.exchange == ex:
                    out.append(r)
                    break
        return out

    async def _run_health_check(self) -> None:
        """Verify each exchange is reachable + authenticated.

        Also writes a heartbeat row to db.meta so external monitoring can
        detect a stalled daemon ("last heartbeat > N minutes ago" → alert).
        """
        from datetime import datetime, timezone

        # A DCA cycle is exercising the same exchange clients right now;
        # skip this heartbeat rather than race the cycle. The 5-min cadence
        # means the next tick still lands well inside any stale-daemon
        # threshold, and a cycle that pins a client (maker wait_for_fill)
        # would otherwise log a spurious "health check failed" alert.
        if self._cycle_in_progress:
            return

        async with self._jobs_lock:
            await self._run_health_check_inner()

    async def _run_health_check_inner(self) -> None:
        from datetime import datetime, timezone

        for ex in self.exchanges:
            try:
                await ex.health_check()
            except Exception as e:
                streak = self._health_fail_streak.get(ex.name, 0) + 1
                self._health_fail_streak[ex.name] = streak
                logger.error(
                    "Health check FAILED for %s (streak=%d): %s",
                    ex.name, streak, e,
                )
                # Alert exactly once, on the check that first crosses the
                # threshold. Below it: treat as a transient blip and stay
                # silent. Above it: we've already paged — don't re-spam every
                # 5 min for the duration of a sustained outage.
                if streak == HEALTH_ALERT_THRESHOLD:
                    await self.notifier.notify_error(
                        f"{ex.name} health check failed",
                        f"{e}\n\n(failed {streak} consecutive checks "
                        f"~{streak * 5}min)",
                    )
            else:
                prior = self._health_fail_streak.get(ex.name, 0)
                if prior >= HEALTH_ALERT_THRESHOLD:
                    logger.info(
                        "Health check RECOVERED for %s after %d consecutive "
                        "failures", ex.name, prior,
                    )
                self._health_fail_streak[ex.name] = 0

        try:
            self.db.set_meta(
                "daemon.last_heartbeat_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.warning("Failed to write daemon heartbeat: %s", e)

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

    NEXT_FIRE_META_KEY = "dca.next_fire_at"

    def _persist_next_fire(self) -> None:
        """Record when the next DCA cycle is due. Read back on startup to
        detect a fire time that passed while the process was down."""
        try:
            job = self._scheduler.get_job("dca_cycle")
            if job is not None and job.next_run_time is not None:
                self.db.set_meta(
                    self.NEXT_FIRE_META_KEY, job.next_run_time.isoformat()
                )
        except Exception as e:
            logger.warning("Failed to persist next fire time: %s", e)

    async def _catch_up_missed_cycle(self) -> None:
        """Run one cycle now if a scheduled fire passed while we were down.

        The in-memory job store + misfire_grace_time only cover IN-PROCESS
        lateness; across a restart the cron computes its next fire from
        `now`, so a daily tenant recreated across its 09:00 fire silently
        lost that day's buy — and a monthly tenant the whole month (audit
        2026-06-10 P2). The risk caps + cross-process cycle lock make an
        immediate catch-up run safe.
        """
        from datetime import datetime, timezone

        raw = None
        try:
            raw = self.db.get_meta(self.NEXT_FIRE_META_KEY)
        except Exception as e:
            logger.warning("Failed to read next-fire meta: %s", e)
        if not raw:
            return
        try:
            due = datetime.fromisoformat(raw)
        except ValueError:
            return
        if due <= datetime.now(timezone.utc):
            logger.warning(
                "Missed DCA cycle detected (was due %s, process was down) — "
                "running catch-up now", raw,
            )
            try:
                await self.notifier.notify_error(
                    "Missed cycle caught up",
                    f"A scheduled buy (due {raw}) was missed while the bot "
                    f"was offline. Running it now.",
                )
            except Exception:
                logger.exception("catch-up notification failed")
            await self._run_dca_cycle()

    async def run_forever(self) -> None:
        """Start the scheduler and block until SIGTERM/SIGINT.

        Performs initial health check on startup so we fail fast on bad creds.
        """
        # Initial health check
        await self._run_health_check()

        self._install_jobs()
        self._scheduler.start()

        # Detect + run a cycle whose fire time passed while we were down,
        # THEN persist the fresh next-fire marker for the next restart.
        try:
            await self._catch_up_missed_cycle()
        except Exception:
            logger.exception("missed-cycle catch-up failed")
        self._persist_next_fire()

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

    # How long shutdown waits for an in-flight cycle before closing clients
    # anyway. Sized to one maker window (tenant default 120s) — the tenant
    # compose sets stop_grace_period above this so docker doesn't SIGKILL
    # us mid-wait. Waiting only happens when a cycle IS in flight.
    SHUTDOWN_CYCLE_WAIT_SECONDS = 120

    async def shutdown(self) -> None:
        logger.info("Shutting down scheduler...")
        self._scheduler.shutdown(wait=False)
        # Closing exchange clients UNDER an in-flight cycle crashes it
        # mid-hop: its cancel/fallback logic never runs, a resting maker
        # order is orphaned on the exchange, and a fill during the restart
        # window never reaches the trades table (daily-cap undercount + tax
        # CSV gap — audit 2026-06-10 P2). Deploys force-recreate containers
        # routinely, so wait (bounded) for the cycle to finish first.
        if self._cycle_in_progress:
            logger.info(
                "DCA cycle in flight — waiting up to %ds before closing "
                "clients", self.SHUTDOWN_CYCLE_WAIT_SECONDS,
            )
            for _ in range(self.SHUTDOWN_CYCLE_WAIT_SECONDS):
                if not self._cycle_in_progress:
                    break
                await asyncio.sleep(1)
            if self._cycle_in_progress:
                logger.warning(
                    "Cycle still in flight after %ds — closing anyway; the "
                    "next cycle's pre-sweep cancels any resting bot order",
                    self.SHUTDOWN_CYCLE_WAIT_SECONDS,
                )
        # Record when the next buy was due so a restart that overshoots it
        # triggers the startup catch-up.
        self._persist_next_fire()
        for ex in self.exchanges:
            try:
                await ex.close()
            except Exception:
                pass
        self.db.close()
        logger.info("Shutdown complete.")
