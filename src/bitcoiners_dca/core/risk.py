"""
RiskManager — circuit breakers + spend caps that wrap the DCA cycle.

Three protections layered before any buy is placed:

  1. Pause state — if the bot was paused (manual or auto), every cycle is
     skipped until resumed. Auto-pause fires after N consecutive failed cycles.

  2. Daily spend cap — total AED spent today (UTC-day) cannot exceed
     `max_daily_aed`. If a scheduled buy would push us over, the cycle is
     clamped to the remaining budget (or skipped if the budget is exhausted).

  3. Single-buy cap — any individual cycle cannot spend more than
     `max_single_buy_aed`, even if the dip overlay multiplies the base amount.

State is persisted in the `meta` table so restarts don't reset the failure
counter or the paused flag. Daily spend is computed live from the `trades`
table (single source of truth — no double counting).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from bitcoiners_dca.persistence.db import Database

logger = logging.getLogger(__name__)


META_PAUSED = "risk.paused"                       # "true" | "false"
META_PAUSED_AT = "risk.paused_at"                 # ISO8601
META_PAUSED_REASON = "risk.paused_reason"
META_CONSECUTIVE_FAILURES = "risk.consecutive_failures"  # integer string


@dataclass
class RiskDecision:
    """Result of evaluating risk before a cycle.

    - `allow=False` means skip the cycle entirely (paused, or daily cap met).
    - `allow=True` with `amount_aed < intended` means clamp the spend.
    - `reasons` contains every factor considered, for audit + notifications.
    """
    allow: bool
    amount_aed: Decimal
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(
        self,
        db: Database,
        max_daily_aed: Optional[Decimal] = None,
        max_single_buy_aed: Optional[Decimal] = None,
        max_consecutive_failures: int = 5,
        # User timezone for daily-cap window boundaries. Defaults to UTC
        # so existing tests / callers behave identically; callers that
        # know the strategy timezone (cli._build_strategy, scheduler)
        # should pass it in so a Dubai customer's "daily" cap resets at
        # local midnight, not 04:00 local (UTC midnight).
        timezone_str: str = "UTC",
    ):
        self.db = db
        self.max_daily_aed = max_daily_aed
        self.max_single_buy_aed = max_single_buy_aed
        self.max_consecutive_failures = max_consecutive_failures
        self.timezone_str = timezone_str

    # === STATE QUERIES ===

    def is_paused(self) -> bool:
        return (self.db.get_meta(META_PAUSED) or "false").lower() == "true"

    def paused_reason(self) -> Optional[str]:
        return self.db.get_meta(META_PAUSED_REASON)

    def consecutive_failures(self) -> int:
        raw = self.db.get_meta(META_CONSECUTIVE_FAILURES)
        return int(raw) if raw and raw.isdigit() else 0

    def daily_spend_aed(self, today_utc: Optional[datetime] = None) -> Decimal:
        """Sum of filled BTC-receiving buys in `amount_quote` for today.

        The "today" boundary is computed in the strategy's timezone (e.g.
        Asia/Dubai → resets at local midnight) by:
          1. taking `now` in user tz
          2. floor to local midnight
          3. converting BOTH bounds back to UTC for the SQL range filter
        Trade timestamps are stored as UTC ISO8601, so a UTC range filter
        is the right comparison even when the user-tz "day" straddles UTC.

        Only counts the AED-spending leg (pair LIKE '%/AED') — same logic
        as Database.total_aed_spent so we don't double-count two-hop
        cycles that persist both legs.
        """
        from zoneinfo import ZoneInfo
        from datetime import timedelta
        try:
            tz = ZoneInfo(self.timezone_str)
        except Exception:
            tz = ZoneInfo("UTC")
        now_local = (today_utc or datetime.now(timezone.utc)).astimezone(tz)
        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        next_local_midnight = local_midnight + timedelta(days=1)
        # ISO8601 of UTC equivalents — SQL compares as strings, fine.
        start_iso = local_midnight.astimezone(timezone.utc).isoformat()
        end_iso = next_local_midnight.astimezone(timezone.utc).isoformat()
        cur = self.db._conn.execute(
            """SELECT COALESCE(SUM(CAST(amount_quote AS REAL)), 0)
               FROM trades
               WHERE side='buy' AND status='filled'
                 AND pair LIKE '%/AED'
                 AND timestamp >= ? AND timestamp < ?""",
            (start_iso, end_iso),
        )
        return Decimal(str(cur.fetchone()[0]))

    # === STATE MUTATIONS ===

    def pause(self, reason: str) -> None:
        self.db.set_meta(META_PAUSED, "true")
        self.db.set_meta(META_PAUSED_AT, datetime.now(timezone.utc).isoformat())
        self.db.set_meta(META_PAUSED_REASON, reason)
        logger.warning("RiskManager paused: %s", reason)

    def resume(self) -> None:
        self.db.set_meta(META_PAUSED, "false")
        self.db.set_meta(META_PAUSED_REASON, "")
        self.db.set_meta(META_CONSECUTIVE_FAILURES, "0")
        logger.info("RiskManager resumed")

    # === EVALUATION ===

    def evaluate(self, intended_amount_aed: Decimal) -> RiskDecision:
        reasons: list[str] = []

        if self.is_paused():
            return RiskDecision(
                allow=False,
                amount_aed=Decimal("0"),
                reasons=[f"paused: {self.paused_reason() or 'unspecified'}"],
            )

        amount = intended_amount_aed

        if self.max_single_buy_aed and amount > self.max_single_buy_aed:
            reasons.append(
                f"clamped to single-buy cap (AED {self.max_single_buy_aed})"
            )
            amount = self.max_single_buy_aed

        if self.max_daily_aed:
            spent = self.daily_spend_aed()
            remaining = self.max_daily_aed - spent
            if remaining <= 0:
                return RiskDecision(
                    allow=False,
                    amount_aed=Decimal("0"),
                    reasons=[f"daily cap reached: AED {spent}/{self.max_daily_aed}"],
                )
            if amount > remaining:
                reasons.append(
                    f"clamped to daily-cap remainder "
                    f"(AED {remaining} of {self.max_daily_aed}, spent {spent})"
                )
                amount = remaining

        if amount <= 0:
            return RiskDecision(
                allow=False, amount_aed=Decimal("0"),
                reasons=reasons + ["computed amount is zero"],
            )

        return RiskDecision(allow=True, amount_aed=amount, reasons=reasons)

    # === LIFECYCLE HOOKS ===

    def record_cycle_result(self, success: bool) -> None:
        """Called after every cycle attempt.

        On success: resets the consecutive-failure counter.
        On failure: increments counter; auto-pauses if threshold reached.
        """
        if success:
            self.db.set_meta(META_CONSECUTIVE_FAILURES, "0")
            return

        n = self.consecutive_failures() + 1
        self.db.set_meta(META_CONSECUTIVE_FAILURES, str(n))
        if n >= self.max_consecutive_failures:
            self.pause(
                f"{n} consecutive failed cycles "
                f"(threshold {self.max_consecutive_failures})"
            )
