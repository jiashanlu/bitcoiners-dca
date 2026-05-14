"""
Shared HTTP plumbing for the hosted Pro API at app.bitcoiners.ae.

Today's call sites — router.py's `_remote_pick` and dashboard.py's
`_remote_backtest` — predate this module and have their own httpx
wrappers (refactor out of scope). New callers go through here so we
don't keep growing per-feature client copies.

Pattern: every remote helper returns the parsed payload on success or
None on any failure (network, 4xx/5xx, JSON parse, `stub:true`). The
caller logs + falls back to local logic — never raises.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_PRO_API_URL = os.environ.get("BITCOINERS_DCA_PRO_API_URL", "").rstrip("/")
_PRO_API_TIMEOUT_SECONDS = float(
    os.environ.get("BITCOINERS_DCA_PRO_API_TIMEOUT", "5")
)


async def remote_funding_readings(
    license_token: Optional[str],
    exchange: str = "okx",
    instrument: str = "BTC-USDT-SWAP",
    hours: int = 24,
) -> Optional[list[dict]]:
    """Hit /api/pro/funding. Returns the list of readings (latest first)
    on success, None on any failure. Caller logs + falls back."""
    from bitcoiners_dca.core import pro_api_status

    if not _PRO_API_URL or not license_token:
        return None
    try:
        import httpx
    except ImportError:
        return None

    url = (
        f"{_PRO_API_URL}/api/pro/funding"
        f"?exchange={exchange}&instrument={instrument}&hours={hours}"
    )
    try:
        async with httpx.AsyncClient(timeout=_PRO_API_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {license_token}"},
            )
    except httpx.HTTPError as e:
        logger.warning("[pro-api] /api/pro/funding call failed: %s", e)
        await pro_api_status.record_fallback("/api/pro/funding", f"network error: {e}")
        return None

    if resp.status_code != 200:
        logger.warning(
            "[pro-api] /api/pro/funding HTTP %s — using local poll",
            resp.status_code,
        )
        await pro_api_status.record_fallback(
            "/api/pro/funding", f"HTTP {resp.status_code}",
        )
        return None

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[pro-api] /api/pro/funding non-JSON: %s", e)
        await pro_api_status.record_fallback("/api/pro/funding", "malformed response")
        return None

    if data.get("stub"):
        await pro_api_status.record_fallback(
            "/api/pro/funding",
            f"server returned stub: {data.get('rationale', 'no rationale')}",
        )
        return None
    if data.get("stale"):
        # Cache empty / cron failure. Bot falls back so monitor still works.
        await pro_api_status.record_fallback(
            "/api/pro/funding", "server cache stale (refresh-funding cron probably late)",
        )
        return None

    readings = data.get("readings")
    if not isinstance(readings, list):
        await pro_api_status.record_fallback(
            "/api/pro/funding", "malformed response (readings missing)",
        )
        return None

    await pro_api_status.record_success("/api/pro/funding")
    return readings
