"""
License expiry watch — warn BEFORE a Pro/Business tenant silently
downgrades to FREE.

Why this exists: on 2026-08-12 the benbois tenant's Pro license lapsed.
Nothing warned anyone; the daemon quietly downgraded to FREE, lost
multi-exchange/USDT routing, burned its remaining AED and risk-paused
after 5 failed cycles. The expiry was only discovered by reading logs.

This module is pure decision logic (no I/O): given the configured token
and a record of which warnings already fired, decide whether a warning
is due now. The scheduler owns the cadence, the notifier owns delivery,
and db.meta owns the fired-warning record — so a daemon restart never
re-spams and a renewed token (new expires_at) naturally re-arms every
threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bitcoiners_dca.core.license import (
    LICENSE_PUBLIC_KEY_HEX,
    LicenseError,
    parse_license_token,
)

# Days-before-expiry at which to warn. 0 = "it has expired". Ordered
# most-severe-first; only the single most severe crossed-but-unwarned
# threshold fires per check (a daemon that wakes up at T-2d sends one
# message, not three).
THRESHOLDS_DAYS: tuple[int, ...] = (0, 3, 14)

# db.meta key holding fired warnings as comma-separated
# "<expires-date>:<threshold>" entries.
META_KEY = "license.expiry_warned"
# Entries kept in the meta value — enough for every threshold of the
# current key plus a couple of renewals of history.
_META_MAX_ENTRIES = 9


@dataclass(frozen=True)
class ExpiryWarning:
    threshold_days: int          # which threshold fired (0 = expired)
    days_left: float             # precise days until expiry (negative = past)
    expires_at: datetime
    customer_id: str
    tier: str

    @property
    def meta_entry(self) -> str:
        return f"{self.expires_at.date().isoformat()}:{self.threshold_days}"


def pending_warning(
    token: str | None,
    already_warned: str | None,
    now: datetime | None = None,
    public_key_hex: str = LICENSE_PUBLIC_KEY_HEX,
) -> ExpiryWarning | None:
    """The single most severe crossed threshold not yet warned, or None.

    Malformed/unsigned tokens return None — the LicenseManager already
    logs those loudly at boot; this watch only covers the valid-but-
    expiring case. Perpetual licenses (no expires_at) never warn.
    """
    if not token:
        return None
    try:
        lic = parse_license_token(token, public_key_hex)
    except LicenseError:
        return None
    if lic.expires_at is None:
        return None

    now = now or datetime.now(UTC)
    days_left = (lic.expires_at - now).total_seconds() / 86_400
    warned = set((already_warned or "").split(","))

    for threshold in THRESHOLDS_DAYS:  # most severe first
        if days_left <= threshold:
            warning = ExpiryWarning(
                threshold_days=threshold,
                days_left=days_left,
                expires_at=lic.expires_at,
                customer_id=lic.customer_id,
                tier=lic.tier.value,
            )
            return None if warning.meta_entry in warned else warning
    return None


def record_warning(already_warned: str | None, warning: ExpiryWarning) -> str:
    """New meta value with this warning appended (bounded, deduped)."""
    entries = [e for e in (already_warned or "").split(",") if e]
    if warning.meta_entry not in entries:
        entries.append(warning.meta_entry)
    return ",".join(entries[-_META_MAX_ENTRIES:])


def customer_message(w: ExpiryWarning) -> str:
    """Telegram text for the tenant's own notification channel."""
    date = w.expires_at.date().isoformat()
    if w.threshold_days == 0:
        return (
            f"🔑 *Your {w.tier.capitalize()} license has EXPIRED* ({date})\n\n"
            f"The bot has downgraded to the Free tier: single exchange, no "
            f"smart routing (idle USDT/USDC will not be used), no advanced "
            f"overlays. DCA continues at the reduced feature set.\n\n"
            f"Hosted subscriptions renew automatically on payment — if you "
            f"see this, the renewal didn't reach your bot; contact "
            f"support@bitcoiners.ae. Self-hosted: renew at "
            f"https://app.bitcoiners.ae"
        )
    days = int(w.days_left)
    return (
        f"🔑 *Your {w.tier.capitalize()} license expires in {days} "
        f"day{'s' if days != 1 else ''}* ({date})\n\n"
        f"If it lapses the bot silently downgrades to Free (single "
        f"exchange, no smart routing). Hosted subscriptions renew "
        f"automatically on payment; self-hosted licenses renew at "
        f"https://app.bitcoiners.ae. Questions: support@bitcoiners.ae"
    )
