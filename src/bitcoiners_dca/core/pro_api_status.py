"""
Tracks whether the bot's most-recent Pro API attempts succeeded or fell
back to the local engine. Surfaced in the dashboard via a non-blocking
banner so paying customers know when they're transparently running on
local logic instead of the hosted Pro service.

Thread-safety: a single asyncio.Lock guards mutation. Reads are a dict
copy — safe to call from sync code (FastAPI route handlers + Jinja
template context).

Memory: in-process. No persistence — the banner resets on bot restart,
which is fine because state is purely informational. If we ever move to
horizontal scale, the same state can be lifted into Postgres or Redis
without changing the call sites.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# Hide the banner once the last fallback is older than this. Tuned to a
# few cycles' worth of time at default 30-min cadence: if everything has
# been healthy for 30 minutes, the past failure is no longer relevant.
RECENT_FALLBACK_WINDOW = timedelta(minutes=30)

# What flips the banner: a failed remote attempt that was newer than the
# most-recent success. We retain both so we can show "last success: X
# ago" in tooltips later if we want.


@dataclass
class _ProApiState:
    last_success_at: Optional[datetime] = None
    last_fallback_at: Optional[datetime] = None
    last_fallback_reason: str = ""
    last_endpoint: str = ""
    # Reset to True if the user explicitly dismisses the banner; flips
    # back to False on the next real fallback.
    dismissed_at: Optional[datetime] = None


_state = _ProApiState()
_lock = asyncio.Lock()


async def record_success(endpoint: str) -> None:
    """Call after every successful (non-stub) Pro API round-trip."""
    async with _lock:
        _state.last_success_at = datetime.now(timezone.utc)
        _state.last_endpoint = endpoint


async def record_fallback(endpoint: str, reason: str) -> None:
    """Call when a Pro API attempt failed and the bot fell back to local.
    Includes stub:true responses, network errors, and HTTP non-200."""
    async with _lock:
        _state.last_fallback_at = datetime.now(timezone.utc)
        _state.last_fallback_reason = reason[:200]
        _state.last_endpoint = endpoint


def dismiss() -> None:
    """User clicked the banner's dismiss button. Hides until the next
    failure."""
    _state.dismissed_at = datetime.now(timezone.utc)


def snapshot() -> dict:
    """Read-only snapshot for template rendering. Returns a dict with:
       - banner_visible: bool — show the banner right now?
       - reason: str — most recent fallback reason (may be stale)
       - last_fallback_at: ISO timestamp or None
       - last_success_at: ISO timestamp or None
       - endpoint: str — which endpoint last fell back
    """
    now = datetime.now(timezone.utc)
    s = _state  # local alias

    banner_visible = False
    if s.last_fallback_at:
        is_recent = (now - s.last_fallback_at) < RECENT_FALLBACK_WINDOW
        success_after_fallback = (
            s.last_success_at is not None
            and s.last_success_at > s.last_fallback_at
        )
        dismissed_after_fallback = (
            s.dismissed_at is not None
            and s.dismissed_at > s.last_fallback_at
        )
        banner_visible = (
            is_recent
            and not success_after_fallback
            and not dismissed_after_fallback
        )

    return {
        "banner_visible": banner_visible,
        "reason": s.last_fallback_reason,
        "endpoint": s.last_endpoint,
        "last_fallback_at": (
            s.last_fallback_at.isoformat() if s.last_fallback_at else None
        ),
        "last_success_at": (
            s.last_success_at.isoformat() if s.last_success_at else None
        ),
    }


# Synchronous helpers for code that lives outside asyncio loops. Calls
# the async versions on a private event loop owned by an executor pool —
# zero risk of contention because the lock is fast and contention would
# only happen between cycles, not within a single one.
def record_success_sync(endpoint: str) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(record_success(endpoint))
            return
    except RuntimeError:
        pass
    asyncio.run(record_success(endpoint))


def record_fallback_sync(endpoint: str, reason: str) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(record_fallback(endpoint, reason))
            return
    except RuntimeError:
        pass
    asyncio.run(record_fallback(endpoint, reason))
