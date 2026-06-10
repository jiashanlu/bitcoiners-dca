"""
Cross-process cycle lock (audit 2026-06-02 #12).

The scheduler daemon and the dashboard Buy-Now run as separate processes on
the same SQLite DB. Without a shared lock both could read the same daily_spend
in a cycle's fill window and each proceed, overspending max_daily_aed by up to
one cycle. try_acquire_cycle_lock serialises them. Two Database instances on
the same file stand in for the two processes here.
"""
from __future__ import annotations

import os
import tempfile

from bitcoiners_dca.persistence.db import Database


def _fresh_db_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "lock.db")


def test_second_holder_is_excluded_then_freed_on_release():
    path = _fresh_db_path()
    daemon = Database(path)
    dashboard = Database(path)  # separate connection == separate process

    assert daemon.try_acquire_cycle_lock() is True
    # The other process can't acquire while the daemon holds it.
    assert dashboard.try_acquire_cycle_lock() is False

    daemon.release_cycle_lock()
    # Now it's free for the dashboard.
    assert dashboard.try_acquire_cycle_lock() is True
    dashboard.release_cycle_lock()


def test_stale_lock_is_reclaimable():
    path = _fresh_db_path()
    db = Database(path)
    assert db.try_acquire_cycle_lock() is True
    # A fresh re-attempt is blocked (lock still young)...
    assert db.try_acquire_cycle_lock() is False
    # ...but a held lock older than the TTL is treated as stale (crashed
    # cycle) and reclaimed — ttl=0 makes any held lock immediately stale.
    assert db.try_acquire_cycle_lock(ttl_seconds=0) is True
    db.release_cycle_lock()


def test_release_is_idempotent():
    path = _fresh_db_path()
    db = Database(path)
    db.release_cycle_lock()  # releasing when not held is a no-op
    assert db.try_acquire_cycle_lock() is True
    db.release_cycle_lock()
    db.release_cycle_lock()  # double release is fine
    assert db.try_acquire_cycle_lock() is True
    db.release_cycle_lock()


# === audit 2026-06-10 P1: owner-checked release ===


def test_release_by_non_owner_does_not_clear_lock():
    """A daemon cycle finishing LATE (after its TTL expired and the dashboard
    re-acquired) must not clear the dashboard's lock — that reopened the
    daily-cap overspend race the lock exists to close."""
    path = _fresh_db_path()
    daemon = Database(path)
    dashboard = Database(path)

    assert daemon.try_acquire_cycle_lock() is True
    # Daemon's TTL "expires" — dashboard reclaims the stale lock.
    assert dashboard.try_acquire_cycle_lock(ttl_seconds=0) is True

    # The overtime daemon cycle now finishes and releases in its finally:.
    daemon.release_cycle_lock()

    # Dashboard's lock must still be held — a third party can't acquire.
    third = Database(path)
    assert third.try_acquire_cycle_lock() is False

    dashboard.release_cycle_lock()
    assert third.try_acquire_cycle_lock() is True


def test_release_clears_legacy_ownerless_lock():
    """Upgrade path: a lock written by the pre-owner code (bare ISO value)
    must still be releasable so a deploy mid-cycle can't wedge the bot."""
    path = _fresh_db_path()
    db = Database(path)
    from datetime import datetime, timezone
    db.set_meta(Database.CYCLE_LOCK_META_KEY,
                datetime.now(timezone.utc).isoformat())

    db.release_cycle_lock()
    assert db.try_acquire_cycle_lock() is True
    db.release_cycle_lock()


def test_default_ttl_covers_three_hop_maker_cycle():
    """3 maker windows × 600s = 1800s < TTL — a slow-but-legitimate cycle
    must not lose its lock mid-flight (the old 900s default did)."""
    assert Database.CYCLE_LOCK_TTL_SECONDS >= 1800
