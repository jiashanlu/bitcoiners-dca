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
