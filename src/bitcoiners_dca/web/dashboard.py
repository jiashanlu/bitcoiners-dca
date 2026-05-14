"""
Customer-facing FastAPI dashboard.

Auth: Cloudflare Access gates the dashboard at the network edge. The
`Cf-Access-Authenticated-User-Email` header is trusted; if it's absent
(direct LAN access during self-hosted Free-tier use), we fall back to
"local-operator".

Pages:
  /                — Overview: KPIs + charts + recent activity
  /strategy        — Edit strategy params, overlays, execution mode
  /exchanges       — Enable/disable + set per-exchange API credentials
  /balances        — Live balance tables across all enabled exchanges
  /prices          — Live BTC ticker across all enabled exchanges
  /trades          — Full trade history (paginated)
  /routes-audit    — Show every viable route at a chosen cycle size
  /settings        — License key, notifications, risk caps
  /healthz         — JSON health check

JSON endpoints (machine-readable):
  /api/stats, /api/balances, /api/prices,
  /api/cumulative-btc, /api/cost-basis-vs-market

HTMX partials (auto-refreshing fragments):
  /htmx/balances, /htmx/prices

Run via:
  uvicorn bitcoiners_dca.web.dashboard:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from bitcoiners_dca.core.license import LicenseManager
from bitcoiners_dca.exchanges.base import Exchange
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.persistence.secrets import (
    SecretStore, SecretStoreError, credentials_for, required_fields,
)
from bitcoiners_dca.utils.config import AppConfig, load_config
from bitcoiners_dca.web.config_writer import ConfigWriter, ConfigWriteError
from bitcoiners_dca.web.jinja_env import make_jinja

logger = logging.getLogger(__name__)

# Pro API base URL — matches the router's resolution. When set AND the
# user has a Pro license key, /backtest tries the hosted /api/pro/backtest
# endpoint first and falls back to the local engine on any failure.
_DASHBOARD_PRO_API_URL = os.environ.get("BITCOINERS_DCA_PRO_API_URL", "").rstrip("/")
_DASHBOARD_PRO_API_TIMEOUT = float(
    os.environ.get("BITCOINERS_DCA_PRO_API_TIMEOUT", "10")
)


async def _remote_backtest(license_token, cfg, points):
    """Call /api/pro/backtest. Returns a BacktestResult or None on
    any failure — caller falls back to local logic."""
    from bitcoiners_dca.core.backtest import BacktestCycle, BacktestResult

    if not _DASHBOARD_PRO_API_URL or not license_token:
        return None
    try:
        import httpx
    except ImportError:
        return None

    body = {
        "amount_aed": float(cfg.base_amount_aed),
        "frequency": cfg.frequency,
        "day_of_week": cfg.day_of_week,
        "taker_fee_pct": float(cfg.taker_fee_pct),
        "dip_overlay_enabled": cfg.dip_overlay_enabled,
        "dip_threshold_pct": float(cfg.dip_threshold_pct),
        "dip_lookback_days": cfg.dip_lookback_days,
        "dip_multiplier": float(cfg.dip_multiplier),
        "points": [
            {"day": p.day.isoformat(), "price": float(p.price)} for p in points
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=_DASHBOARD_PRO_API_TIMEOUT) as client:
            resp = await client.post(
                f"{_DASHBOARD_PRO_API_URL}/api/pro/backtest",
                headers={"Authorization": f"Bearer {license_token}"},
                json=body,
            )
    except httpx.HTTPError as e:
        logger.warning("[pro-api] /api/pro/backtest call failed: %s", e)
        return None

    if resp.status_code != 200:
        logger.warning(
            "[pro-api] /api/pro/backtest HTTP %s — using local engine",
            resp.status_code,
        )
        return None

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[pro-api] /api/pro/backtest non-JSON: %s", e)
        return None

    if data.get("stub"):
        logger.info(
            "[pro-api] /api/pro/backtest stub:true (%s) — using local engine",
            data.get("rationale", "no rationale"),
        )
        return None

    # Translate JSON cycles back into BacktestCycle/Result. Decimals on the
    # way out keep the template's existing %.0f / %.4f formatting happy.
    from datetime import date as _date
    try:
        cycles = [
            BacktestCycle(
                day=_date.fromisoformat(c["day"]),
                price_aed=Decimal(str(c["price_aed"])),
                aed_spent=Decimal(str(c["aed_spent"])),
                btc_bought=Decimal(str(c["btc_bought"])),
                overlay_applied=bool(c.get("overlay_applied", False)),
            )
            for c in data.get("cycles", [])
        ]
        return BacktestResult(
            config=cfg,
            cycles=cycles,
            start_day=_date.fromisoformat(data["start_day"]) if data.get("start_day") else None,
            end_day=_date.fromisoformat(data["end_day"]) if data.get("end_day") else None,
        )
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("[pro-api] malformed /api/pro/backtest response: %s", e)
        return None


CF_USER_HEADER = "Cf-Access-Authenticated-User-Email"


def _authenticated_user(request: Request) -> str:
    """Read the authenticated user from the CF Access / proxy header.

    In production (DCA_REQUIRE_CF_HEADER=1), the absence of this header is
    a security failure: it means the request reached the dashboard without
    going through the bitcoiners-app proxy or Cloudflare Access. We refuse
    the request via _enforce_auth_gate() before any handler runs; this
    function only ever sees an authenticated request.

    In dev/self-host (the default), we fall back to "local-operator" so
    Free-tier users running on their own machine don't need to set up
    Cloudflare Access at all.
    """
    return request.headers.get(CF_USER_HEADER, "local-operator")


def _require_cf_header() -> bool:
    return os.environ.get("DCA_REQUIRE_CF_HEADER", "").strip().lower() in ("1", "true", "yes")


class _CFGateMiddleware(BaseHTTPMiddleware):
    """Refuse any request missing the CF Access user header when the env
    flag DCA_REQUIRE_CF_HEADER=1 is set (production hosted mode).

    Skips /healthz so the container healthcheck still works internally.
    """

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if _require_cf_header() and not request.headers.get(CF_USER_HEADER):
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("Unauthorized: missing proxy header", status_code=401)
        return await call_next(request)


class _OriginCSRFMiddleware(BaseHTTPMiddleware):
    """Same-site enforcement for state-changing requests.

    On every non-safe method (POST/PUT/PATCH/DELETE), require that either
    `Origin` matches the request's host, OR `Referer` starts with the
    request's origin. Refuses the request with 403 otherwise.

    This is the simplest defense against browser-driven CSRF: an attacker
    page on another domain can submit a form to us using the user's cookies
    (CF Access JWT, etc.), but the browser will set `Origin` to the
    attacker's domain on the cross-site POST. We block on mismatch.

    Doesn't break direct API/scripting use because curl/wget don't send an
    Origin header for cross-site contexts — only browsers do. If both
    Origin and Referer are absent, we let it through (likely server-to-
    server or a sanitized curl call).
    """
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    @staticmethod
    def _host_of(url_like: str) -> str:
        """Return just the hostname (no scheme, no port) from a URL-ish string."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url_like)
            return (parsed.hostname or "").lower()
        except Exception:
            return ""

    async def dispatch(self, request: Request, call_next):
        if request.method in self.SAFE_METHODS:
            return await call_next(request)

        # Trust requests that came through bitcoiners-app's proxy. The proxy
        # is server-side; only it can set this header. The proxy already
        # enforces a valid Auth.js session before forwarding, which is the
        # primary CSRF defense — a cross-site attacker can't initiate a
        # POST that the proxy then "blesses" because they don't have the
        # auth cookie for app.bitcoiners.ae.
        if request.headers.get("cf-access-authenticated-user-email"):
            return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        host = request.headers.get("host", "")
        scheme = request.url.scheme
        # The X-Forwarded-Prefix is set by the bitcoiners-app proxy; treat
        # those requests as coming from the proxy host, not the upstream.
        # We accept the proxy's forwarded values.
        fwd_host = request.headers.get("x-forwarded-host")

        # Compare by hostname (case-insensitive, ignore port + scheme) so
        # a proxy that omits the port or differs on http/https doesn't
        # spuriously 403. The cookie-bound CSRF semantics still hold:
        # same registered domain, same browser-origin.
        allowed_hosts = {self._host_of(f"//{host}") for host in {host, fwd_host} if host}
        allowed_hosts.discard("")

        if origin:
            if self._host_of(origin) not in allowed_hosts:
                return JSONResponse(
                    {"error": "csrf",
                     "detail": f"origin host {self._host_of(origin)!r} not in {allowed_hosts}"},
                    status_code=403,
                )
            return await call_next(request)
        if referer:
            if self._host_of(referer) not in allowed_hosts:
                return JSONResponse(
                    {"error": "csrf",
                     "detail": f"referer host {self._host_of(referer)!r} not in {allowed_hosts}"},
                    status_code=403,
                )
            return await call_next(request)
        # No Origin AND no Referer: likely server-to-server / curl. Allow.
        return await call_next(request)


def create_app(
    config_path: str | Path | None = None,
    config: Optional[AppConfig] = None,
    db: Optional[Database] = None,
    exchanges: Optional[list[Exchange]] = None,
) -> FastAPI:
    """Build a FastAPI app. Dependencies are lazily loaded on first request
    when not passed explicitly — supports both standalone uvicorn launch and
    in-process embedding (tests, scheduler co-host).

    Path resolution order for `config_path`:
      1. Explicit argument (tests / programmatic use)
      2. `DCA_DASHBOARD_CONFIG` env var (set by the `dashboard` CLI command
         when launching uvicorn from an import string — uvicorn can't pass
         kwargs through to the module-level factory)
      3. Default ./config.yaml
    """
    if config_path is None:
        config_path = os.environ.get("DCA_DASHBOARD_CONFIG", "./config.yaml")

    app = FastAPI(
        title="bitcoiners-dca dashboard",
        description="Self-service operations dashboard.",
        version="0.6.0",
    )
    # CSRF protection: same-site origin check on all state-changing requests.
    # Stops a malicious page on attacker.example from POSTing to /controls/*
    # using the user's auto-sent CF Access cookie. Combined with the existing
    # CF Access email-OTP layer this is adequate for the hosted threat model.
    app.add_middleware(_OriginCSRFMiddleware)
    # Outer gate: when running in the hosted multi-tenant setup, refuse any
    # request that didn't come through the bitcoiners-app proxy (which sets
    # Cf-Access-Authenticated-User-Email). Opt-in via env so self-host /
    # Free tier still works without it.
    app.add_middleware(_CFGateMiddleware)
    jinja = make_jinja()
    state: dict = {
        "config_path": str(config_path),
        "config": config,
        "db": db,
        "exchanges": exchanges,
        "secrets": None,
    }

    # === Lazy resolvers ===

    def _config() -> AppConfig:
        if state["config"] is None:
            state["config"] = load_config(state["config_path"])
        return state["config"]

    def _refresh_config() -> AppConfig:
        """Force-reload after a write."""
        state["config"] = load_config(state["config_path"])
        # Drop the cached exchange list too; credentials may have changed
        state["exchanges"] = None
        return state["config"]

    def _db() -> Database:
        if state["db"] is None:
            state["db"] = Database(_config().persistence.db_path)
        return state["db"]

    def _secrets() -> Optional[SecretStore]:
        """Optional — None if DCA_SECRETS_KEY isn't set."""
        if state["secrets"] is None:
            try:
                state["secrets"] = SecretStore(_config().persistence.db_path)
            except SecretStoreError as e:
                logger.warning("SecretStore unavailable: %s", e)
                return None
        return state["secrets"]

    def _exchanges() -> list[Exchange]:
        if state["exchanges"] is None:
            state["exchanges"] = _build_dashboard_exchanges(_config(), _secrets())
        return state["exchanges"]

    def _license() -> LicenseManager:
        cfg = _config()
        return LicenseManager.from_config(cfg.license.tier, cfg.license.key)

    def _prefix(request: Request) -> str:
        """Strip trailing slash; "" if not behind a reverse proxy."""
        return request.headers.get("x-forwarded-prefix", "").rstrip("/")

    def _redirect(request: Request, path: str) -> RedirectResponse:
        """303 redirect that honours the iframe-proxy prefix so the
        browser navigates to /dca/console/<path> instead of escaping to
        the parent app's root."""
        return RedirectResponse(_prefix(request) + path, status_code=303)

    def _bot_status() -> dict:
        """One-stop status the banner + Overview card both read.

        Three top-line states:
          - "paused" — risk manager paused (auto after N failures or
                       manual via /controls/pause)
          - "dry_run" — running normally but config.dry_run=true; no real
                        orders placed even when cron fires
          - "live"   — running normally with real orders

        `heartbeat_age_seconds` is the time since the daemon last wrote
        `daemon.last_heartbeat_at` (refreshed every 5 minutes by the
        health-check job). None means "no heartbeat yet" — usually the
        daemon hasn't started. `heartbeat_stale` is True when the last
        heartbeat is older than 15 minutes (3× the cron interval).
        """
        from datetime import datetime, timezone
        try:
            from bitcoiners_dca.core.risk import RiskManager, META_PAUSED_REASON
            db = _db()
            paused = db.get_meta("risk.paused") == "true"
            reason = db.get_meta(META_PAUSED_REASON) or None
            hb_raw = db.get_meta("daemon.last_heartbeat_at")
        except Exception:
            paused = False
            reason = None
            hb_raw = None

        hb_age = None
        hb_stale = False
        if hb_raw:
            try:
                hb_dt = datetime.fromisoformat(hb_raw)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                hb_age = int((datetime.now(timezone.utc) - hb_dt).total_seconds())
                hb_stale = hb_age > 900
            except Exception:
                pass

        base = {
            "heartbeat_age_seconds": hb_age,
            "heartbeat_stale": hb_stale,
        }
        if paused:
            return {"state": "paused", "reason": reason, **base}
        if _config().dry_run:
            return {"state": "dry_run", "reason": None, **base}
        return {"state": "live", "reason": None, **base}

    def _ctx(request: Request, **extra) -> dict:
        cfg = _config()
        # When proxied behind bitcoiners-app's /dca/console/[[...path]],
        # the proxy sets X-Forwarded-Prefix=/dca/console. Templates use
        # this to render all internal links + htmx URLs as
        # /dca/console/<path> so nav stays inside the iframe. Empty
        # string for direct LAN/Free-tier access — links resolve to /.
        prefix = _prefix(request)
        # First-visit welcome: no trades yet AND dry-run on. Once the
        # customer goes live OR a trade lands, the banner stops rendering.
        welcome = False
        try:
            if cfg.dry_run:
                cnt = _db()._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                welcome = (cnt == 0)
        except Exception:
            welcome = False
        # Orphan funds banner: cleared once user clicks Acknowledge.
        orphan = None
        try:
            import json as _json
            raw = _db().get_meta("multi_hop.last_orphan")
            ack = _db().get_meta("multi_hop.orphan_acknowledged_at")
            if raw:
                parsed = _json.loads(raw)
                if not ack or ack < parsed.get("ts", ""):
                    orphan = parsed
        except Exception:
            orphan = None

        return {
            "request": request,
            "user_email": _authenticated_user(request),
            "license_tier": _license().tier.value,
            "config": cfg,
            "prefix": prefix,
            "bot_status": _bot_status(),
            "welcome": welcome,
            "orphan": orphan,
            "flash": extra.pop("flash", None),
            "active": extra.pop("active", ""),
            **extra,
        }

    @app.post("/orphan/acknowledge", response_class=HTMLResponse)
    async def orphan_acknowledge(request: Request):
        """Clear the orphan banner once the operator has cleaned up the
        stuck intermediate-currency funds. Writes an acknowledged-at
        timestamp; _ctx hides the banner when ack >= last_orphan.ts."""
        from datetime import datetime, timezone
        _db().set_meta(
            "multi_hop.orphan_acknowledged_at",
            datetime.now(timezone.utc).isoformat(),
        )
        return _redirect(request, "/")

    # === PAGES ===

    def _decorate_trades(rows, user_tz: str) -> list[dict]:
        """Collapse multi-hop legs into one row per cycle.

        A two-hop AED→USDT→BTC cycle persists two trade rows (USDT/AED
        then BTC/USDT). We show those as a SINGLE row:
          - timestamp = first leg
          - route     = "AED → USDT → BTC"
          - spent     = amount_quote of the first leg (e.g. 54.79 AED)
          - received  = amount_base of the LAST leg (e.g. 0.000184 BTC)
          - price     = spent / received (effective AED/BTC)
          - status    = worst-of group ('filled' only if every leg filled)
          - order_id  = comma-joined for traceability

        Direct BTC/AED or USDT-direct BTC/USDT cycles render as-is. Group
        boundary: adjacent rows on the same exchange within 10 seconds.
        """
        from datetime import datetime, timezone as _tz
        from decimal import Decimal as _D
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(user_tz)
        except Exception:
            tz = ZoneInfo("Asia/Dubai")

        sorted_asc = sorted([dict(r) for r in rows], key=lambda r: r["timestamp"])
        decorated: list[dict] = []
        cur_group: list[dict] = []

        def _parse(ts: str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt

        def _dec(v) -> _D:
            try:
                return _D(str(v)) if v not in (None, "") else _D(0)
            except Exception:
                return _D(0)

        def _close_group(group):
            if not group:
                return
            first = group[0]
            last = group[-1]
            # Build route currencies from the chain of pairs. Each pair
            # is "BASE/QUOTE"; first pair gives us the starting quote
            # (the currency we spent), each base is the next received.
            currencies: list[str] = []
            for p in [g["pair"] for g in group]:
                base, quote = (p.split("/") + [""])[:2]
                if not currencies:
                    currencies.append(quote)
                currencies.append(base)
            route_label = " → ".join(currencies)
            spent = _dec(first["amount_quote"])
            received = _dec(last["amount_base"])
            spent_ccy = (first["pair"].split("/") + [""])[1]
            recv_ccy = (last["pair"].split("/") + [""])[0]
            effective_price = (spent / received) if received > 0 else _D(0)
            # Status: filled iff every leg filled; otherwise show the
            # worst non-filled status (e.g. one leg cancelled).
            statuses = {g.get("status") for g in group}
            if statuses == {"filled"}:
                merged_status = "filled"
            elif "cancelled" in statuses:
                merged_status = "cancelled"
            else:
                merged_status = next(iter(statuses - {"filled"}), "filled")
            decorated.append({
                "local_ts": first["local_ts"],
                "exchange": first["exchange"],
                "route": route_label,
                "is_multi_hop": len(group) > 1,
                "spent": spent,
                "spent_ccy": spent_ccy,
                "received": received,
                "received_ccy": recv_ccy,
                "effective_price": effective_price,
                "price_pair": f"{spent_ccy}/{recv_ccy}",
                "status": merged_status,
                "order_id": ", ".join(str(g.get("order_id") or "") for g in group),
            })

        for r in sorted_asc:
            dt = _parse(r["timestamp"])
            r["local_ts"] = dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            r["ts_dt"] = dt
            if (
                cur_group
                and r["exchange"] == cur_group[-1]["exchange"]
                and (dt - cur_group[-1]["ts_dt"]).total_seconds() <= 10
            ):
                cur_group.append(r)
            else:
                _close_group(cur_group)
                cur_group = [r]
        _close_group(cur_group)
        decorated.reverse()
        return decorated

    def _overview_data():
        """Shared between full /overview page and the /htmx/overview-stats
        partial, so they always render with the same shape."""
        db = _db()
        cfg = _config()
        user_tz = cfg.strategy.timezone or "Asia/Dubai"
        total_btc = db.total_btc_bought()
        total_aed = db.total_aed_spent()
        avg_price = total_aed / total_btc if total_btc > 0 else Decimal(0)
        # Pull 24 rows so multi-hop cycles (2 rows each) still surface
        # ~10 distinct cycles after merging.
        rows = db._conn.execute(
            """SELECT timestamp, exchange, pair, side, amount_quote,
                      amount_base, price_avg, status, order_id
               FROM trades
               WHERE side='buy' AND status='filled'
               ORDER BY timestamp DESC LIMIT 24"""
        ).fetchall()
        recent = _decorate_trades(rows, user_tz)[:12]
        arb_count = db._conn.execute(
            "SELECT COUNT(*) FROM arbitrage_log WHERE alerted=1"
        ).fetchone()[0]
        cycle_count = db._conn.execute(
            "SELECT COUNT(*) FROM cycle_log"
        ).fetchone()[0]
        return {
            "total_btc": total_btc,
            "total_aed": total_aed,
            "avg_price": avg_price,
            "recent": recent,
            "user_tz": user_tz,
            "arb_count": arb_count,
            "cycle_count": cycle_count,
        }

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        return HTMLResponse(jinja.get_template("overview.html").render(_ctx(
            request, active="overview", **_overview_data(),
        )))

    @app.get("/htmx/overview-stats", response_class=HTMLResponse)
    async def htmx_overview_stats(request: Request):
        """Stats grid + recent trades, polled every 20s by the overview page
        so totals + the recent-trades table update without a full reload."""
        cfg = _config()
        return HTMLResponse(jinja.get_template("partials/overview_stats.html").render(
            config=cfg, prefix=_prefix(request), **_overview_data(),
        ))

    @app.get("/htmx/status-banner", response_class=HTMLResponse)
    async def htmx_status_banner(request: Request):
        """Status banner partial — polled every 20s so pause/dry-run/live
        toggles and heartbeat-staleness surface without a full reload."""
        return HTMLResponse(jinja.get_template("partials/status_banner.html").render(
            bot_status=_bot_status(), prefix=_prefix(request),
        ))

    @app.get("/htmx/trades-list", response_class=HTMLResponse)
    async def htmx_trades_list(request: Request, page: int = Query(1, ge=1)):
        """Trades table partial — same shape as the full /trades page body
        but without the surrounding layout, for in-place htmx refresh."""
        per_page = 50
        db = _db()
        cfg = _config()
        user_tz = cfg.strategy.timezone or "Asia/Dubai"
        total = db._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        rows = db._conn.execute(
            """SELECT timestamp, exchange, pair, side, amount_quote,
                      amount_base, price_avg, status, order_id
               FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            (per_page, (page - 1) * per_page),
        ).fetchall()
        decorated = _decorate_trades(rows, user_tz)
        last_page = max(1, (total + per_page - 1) // per_page)
        return HTMLResponse(jinja.get_template("partials/trades_list.html").render(
            trades=decorated, total=total, page=page, user_tz=user_tz,
            last_page=last_page, prev_page=max(1, page - 1),
            next_page=min(last_page, page + 1), prefix=_prefix(request),
        ))

    @app.get("/strategy", response_class=HTMLResponse)
    async def strategy_page(request: Request):
        return HTMLResponse(jinja.get_template("strategy.html").render(_ctx(
            request, active="strategy",
        )))

    @app.post("/strategy", response_class=HTMLResponse)
    async def strategy_save(request: Request):
        from decimal import Decimal, InvalidOperation
        from bitcoiners_dca.core.strategy import derive_per_cycle

        form = await request.form()
        frequency = form.get("frequency", "weekly")
        budget_period = form.get("budget_period", "cycle")
        try:
            every_n_hours = max(1, int(form.get("every_n_hours", "1") or "1"))
        except ValueError:
            every_n_hours = 1
        # Parse budget amount; fall back to legacy amount_aed if budget_amount
        # is missing (older clients before this UX shipped).
        raw_budget = (form.get("budget_amount") or form.get("amount_aed") or "0").strip()
        try:
            budget_amount = Decimal(raw_budget)
        except InvalidOperation:
            budget_amount = Decimal(0)
        amount_aed = derive_per_cycle(budget_amount, budget_period, frequency, every_n_hours)

        # Build patch dict from form
        patch = {
            "strategy.amount_aed": str(amount_aed),
            "strategy.budget_amount": str(budget_amount),
            "strategy.budget_period": budget_period,
            "strategy.frequency": frequency,
            "strategy.every_n_hours": every_n_hours,
            "strategy.day_of_week": form.get("day_of_week", "monday"),
            "strategy.time": form.get("time", "09:00"),
            "strategy.timezone": form.get("timezone", "Asia/Dubai"),
            "execution.mode": form.get("execution_mode", "taker"),
            "execution.maker.timeout_seconds": int(form.get("maker_timeout", 600)),
            "overlays.buy_the_dip.enabled": form.get("dip_enabled") == "on",
            "overlays.buy_the_dip.threshold_pct": str(form.get("dip_threshold", "-10")),
            "overlays.buy_the_dip.multiplier": str(form.get("dip_multiplier", "2.0")),
            "overlays.buy_the_dip.lookback_days": int(form.get("dip_lookback", 7)),
            "overlays.volatility_weighted.enabled": form.get("vol_enabled") == "on",
            "overlays.time_of_day.enabled": form.get("tod_enabled") == "on",
            "overlays.drawdown_aware.enabled": form.get("dd_enabled") == "on",
            "routing.enable_two_hop": form.get("two_hop") == "on",
            "routing.enable_cross_exchange_alerts": form.get("cross_alerts") == "on",
            "routing.preferred_exchange":
                form.get("preferred_exchange") or None,
            # "" and "0"/"0.0" both mean "no cap". Treating "0" as a real
            # daily cap silently halts every cycle, which has caught real
            # customers (and Ben).
            "risk.max_daily_aed": _parse_risk_cap(form.get("max_daily_aed")),
            "risk.max_single_buy_aed": _parse_risk_cap(form.get("max_single_buy_aed")),
        }

        # Server-side sanity on amount_aed. Catches the trap that bit a real
        # customer (15,000 AED/cycle hourly = 360k AED/day intent). Two
        # guardrails:
        #   1. Hard ceiling: refuse > 100,000 AED per cycle, period. Anyone
        #      who genuinely DCAs 100k+ per cycle is in private banking, not
        #      this UX.
        #   2. Soft ceiling: 5,000 AED/cycle requires explicit
        #      max_single_buy_aed to be set. Without it the user is one
        #      misclick away from sweeping a balance.
        max_single_cap = _parse_risk_cap(form.get("max_single_buy_aed"))
        if amount_aed > Decimal("100000"):
            flash = {"kind": "err",
                     "message": f"Per-cycle amount {amount_aed} AED exceeds the 100,000 AED hard ceiling. Lower the budget or stretch the cadence. Saving refused."}
            return HTMLResponse(jinja.get_template("strategy.html").render(_ctx(
                request, active="strategy", flash=flash,
            )))
        if amount_aed > Decimal("5000") and not max_single_cap:
            flash = {"kind": "err",
                     "message": (
                         f"Per-cycle amount is {amount_aed} AED. Set a "
                         "Max single-buy cap (AED) below to confirm this is "
                         "intended — otherwise saving is refused. The cap "
                         "stops a typo or misclick from sweeping your wallet."
                     )}
            return HTMLResponse(jinja.get_template("strategy.html").render(_ctx(
                request, active="strategy", flash=flash,
            )))
        # Auto-suggest a max_daily_aed if it's not set. Heuristic: 2x daily
        # spend at the configured cadence. User can override later.
        if not _parse_risk_cap(form.get("max_daily_aed")):
            cycles_per_day = {
                "hourly": Decimal(24) / Decimal(every_n_hours),
                "daily": Decimal(1),
                "weekly": Decimal("0.143"),  # 1/7
                "monthly": Decimal("0.033"),  # 1/30
            }.get(frequency, Decimal(1))
            suggested_daily = (amount_aed * cycles_per_day * Decimal(2)).quantize(Decimal("1"))
            patch["risk.max_daily_aed"] = str(suggested_daily)

        flash = _apply_patch(state["config_path"], patch, _refresh_config)
        return HTMLResponse(jinja.get_template("strategy.html").render(_ctx(
            request, active="strategy", flash=flash,
        )))

    @app.get("/exchanges", response_class=HTMLResponse)
    async def exchanges_page(request: Request):
        sec = _secrets()
        creds: dict[str, dict[str, str]] = {}
        if sec:
            for ex in ("okx", "binance", "bitoasis"):
                stored = credentials_for(sec, ex)
                # Display redacted
                creds[ex] = {
                    field: _redact(value)
                    for field, value in stored.items()
                }
        # Surface results of the "Test connection" button.
        test_ok = request.query_params.get("ok")
        test_err = request.query_params.get("err")
        flash = None
        if test_ok:
            flash = {"kind": "ok", "message": f"{test_ok}: connection OK — API key is valid and scoped correctly."}
        elif test_err:
            ex_name, _, msg = test_err.partition(":")
            flash = {"kind": "err", "message": f"{ex_name} connection failed: {msg or 'unknown error'}"}
        # The bot's outbound IP — customer needs this to whitelist the
        # bot host on their exchange API key. Fetch once and cache; on
        # error fall through silently (worst case the customer sees
        # "—" and has to ask support).
        outbound_ip = state.get("_outbound_ip")
        if not outbound_ip:
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=4.0) as client:
                    r = await client.get("https://api.ipify.org")
                    outbound_ip = r.text.strip()
                    state["_outbound_ip"] = outbound_ip
            except Exception:
                outbound_ip = None
        return HTMLResponse(jinja.get_template("exchanges.html").render(_ctx(
            request, active="exchanges", flash=flash,
            credentials=creds, secrets_available=sec is not None,
            required_fields=required_fields,
            outbound_ip=outbound_ip,
        )))

    @app.post("/exchanges/{name}/toggle", response_class=HTMLResponse)
    async def exchange_toggle(request: Request, name: str):
        if name not in ("okx", "binance", "bitoasis"):
            raise HTTPException(404)
        form = await request.form()
        enabled = form.get("enabled") == "on"
        flash = _apply_patch(state["config_path"], {
            f"exchanges.{name}.enabled": enabled,
        }, _refresh_config)
        return _redirect(request, "/exchanges")

    @app.post("/exchanges/{name}/test", response_class=HTMLResponse)
    async def exchange_test_connection(request: Request, name: str):
        """Verify the saved API key works without placing any orders.

        Calls the adapter's health_check() which reads /account or
        /balances — read-only. Surfaces success or the exchange's
        error message so the customer knows immediately whether their
        key is valid + scoped correctly, instead of waiting for the
        next failed cycle.
        """
        if name not in ("okx", "binance", "bitoasis"):
            raise HTTPException(404)
        # Find this adapter in the cached exchange list (re-instantiated
        # after credentials save).
        target = None
        for ex in _exchanges():
            if ex.name == name:
                target = ex
                break
        if target is None:
            flash = {"kind": "err",
                     "message": f"{name} is not enabled or has no credentials yet. Save credentials first."}
            return _redirect(request, f"/exchanges?flash={flash['kind']}:{flash['message'][:140]}")
        try:
            await target.health_check()
            return _redirect(request, f"/exchanges?ok={name}")
        except Exception as e:
            return _redirect(request, f"/exchanges?err={name}:{str(e)[:160]}")

    @app.post("/exchanges/{name}/credentials", response_class=HTMLResponse)
    async def exchange_credentials(request: Request, name: str):
        if name not in ("okx", "binance", "bitoasis"):
            raise HTTPException(404)
        sec = _secrets()
        if sec is None:
            flash = {"kind": "err",
                     "message": "Secret store not configured. Set DCA_SECRETS_KEY in .env"}
        else:
            form = await request.form()
            updated = 0
            for field in required_fields(name):
                val = form.get(field, "").strip()
                if val and val != "***":  # don't overwrite when user leaves redacted placeholder
                    sec.set(f"{name}.{field}", val)
                    updated += 1
            flash = {"kind": "ok", "message": f"Saved {updated} credential field(s) for {name}"}
            # Force exchange re-instantiation on next request
            state["exchanges"] = None
        return _redirect(request, "/exchanges")

    @app.get("/balances", response_class=HTMLResponse)
    async def balances_page(request: Request):
        return HTMLResponse(jinja.get_template("balances.html").render(_ctx(
            request, active="balances",
        )))

    # Process-local TTL cache for the live exchange fetches that drag
    # /htmx/balances and /htmx/prices to ~15s. Both pages auto-refresh
    # via htmx every ~60s and the underlying balance/ticker doesn't move
    # meaningfully on a second-by-second basis, so a 45s TTL keeps the
    # UX feeling live but cuts wait time on cached hits to near-zero.
    # Cache is invalidated by `?fresh=1` for explicit refresh buttons.
    _live_cache: dict[str, tuple[float, object]] = {}
    _LIVE_CACHE_TTL = 45.0

    def _cache_get(key: str):
        import time
        entry = _live_cache.get(key)
        if not entry:
            return None
        ts, val = entry
        if time.time() - ts > _LIVE_CACHE_TTL:
            return None
        return val

    def _cache_set(key: str, val):
        import time
        _live_cache[key] = (time.time(), val)

    @app.get("/htmx/balances", response_class=HTMLResponse)
    async def htmx_balances(request: Request):
        rows = None
        cached_at = None
        if request.query_params.get("fresh") != "1":
            entry = _live_cache.get("balances")
            if entry:
                ts, val = entry
                import time as _t
                if _t.time() - ts <= _LIVE_CACHE_TTL:
                    rows = val
                    cached_at = ts
        if rows is None:
            results = await asyncio.gather(
                *[_safe_get_balances(ex) for ex in _exchanges()],
                return_exceptions=True,
            )
            rows = []
            for r in results:
                if isinstance(r, Exception):
                    continue
                name, bals = r
                for b in bals if isinstance(bals, list) else []:
                    if b.get("free", 0) or b.get("total", 0):
                        rows.append({"exchange": name, **b})
            _cache_set("balances", rows)
        return HTMLResponse(jinja.get_template("partials/balances_table.html").render(
            balances=rows, now=datetime.utcnow(), prefix=_prefix(request),
        ))

    @app.get("/prices", response_class=HTMLResponse)
    async def prices_page(request: Request):
        pair = request.query_params.get("pair", "BTC/AED")
        return HTMLResponse(jinja.get_template("prices.html").render(_ctx(
            request, active="prices", pair=pair,
        )))

    @app.get("/htmx/prices", response_class=HTMLResponse)
    async def htmx_prices(request: Request):
        pair = request.query_params.get("pair", "BTC/AED")
        rows = None
        if request.query_params.get("fresh") != "1":
            import time as _t
            entry = _live_cache.get(f"prices:{pair}")
            if entry and _t.time() - entry[0] <= _LIVE_CACHE_TTL:
                rows = entry[1]
        if rows is not None:
            return HTMLResponse(jinja.get_template("partials/prices_table.html").render(
                prices=rows, pair=pair, now=datetime.utcnow(), prefix=_prefix(request),
            ))
        results = await asyncio.gather(
            *[_safe_get_ticker(ex, pair) for ex in _exchanges()],
            return_exceptions=True,
        )
        rows = []
        for r in results:
            if isinstance(r, Exception):
                continue
            name, t = r
            rows.append({"exchange": name, **t})
        _cache_set(f"prices:{pair}", rows)
        return HTMLResponse(jinja.get_template("partials/prices_table.html").render(
            prices=rows, pair=pair, now=datetime.utcnow(), prefix=_prefix(request),
        ))

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(request: Request, page: int = Query(1, ge=1)):
        per_page = 50
        db = _db()
        cfg = _config()
        user_tz = cfg.strategy.timezone or "Asia/Dubai"
        total = db._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        rows = db._conn.execute(
            """SELECT timestamp, exchange, pair, side, amount_quote,
                      amount_base, price_avg, status, order_id
               FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            (per_page, (page - 1) * per_page),
        ).fetchall()

        decorated = _decorate_trades(rows, user_tz)
        last_page = max(1, (total + per_page - 1) // per_page)
        # Surface a flash if we landed here from /controls/buy-now
        flash = None
        if request.query_params.get("ran"):
            flash = {"kind": "ok", "message": "Buy-now cycle ran. The result should be at the top of the table below."}
        elif request.query_params.get("error"):
            flash = {"kind": "err",
                     "message": f"Buy-now failed: {request.query_params['error']}"}
        return HTMLResponse(jinja.get_template("trades.html").render(_ctx(
            request, active="trades", flash=flash,
            trades=decorated, total=total, page=page, user_tz=user_tz,
            last_page=last_page, prev_page=max(1, page - 1),
            next_page=min(last_page, page + 1),
        )))

    @app.get("/routes-audit", response_class=HTMLResponse)
    async def routes_audit_page(request: Request):
        amount = Decimal(request.query_params.get("amount", "500"))
        from bitcoiners_dca.core.router import SmartRouter
        cfg = _config()
        router = SmartRouter(
            enable_two_hop=cfg.routing.enable_two_hop,
            intermediates=cfg.routing.intermediates,
            enable_cross_exchange_alerts=cfg.routing.enable_cross_exchange_alerts,
            cross_exchange_min_size_aed=cfg.routing.cross_exchange_min_size_aed,
            cross_exchange_withdrawal_costs=cfg.routing.cross_exchange_withdrawal_costs,
            preferred_exchange=cfg.routing.preferred_exchange,
            preferred_bonus_pct=cfg.routing.preferred_bonus_pct,
        )
        decision = None
        error = None
        try:
            decision = await router.pick(
                _exchanges(), pair="BTC/AED",
                required_quote_amount=amount,
                license_token=getattr(getattr(cfg, "license", None), "key", None),
            )
            # Audit shows only routes that start from AED. Intermediate-
            # direct routes (e.g. BTC/USDT when there's idle USDT) are an
            # execution optimization, not an AED→BTC alternative the user
            # is comparing here — they pollute the table with rows in the
            # wrong unit ("balance 2.18 AED" was really 2.18 USDT).
            if decision:
                aed_routes = [
                    c for c in [decision.chosen] + decision.alternatives
                    if c.route.hops and c.route.hops[0].pair.endswith("/AED")
                ]
                if aed_routes:
                    decision.chosen = aed_routes[0]
                    decision.alternatives = aed_routes[1:]
        except Exception as e:
            error = str(e)[:200]
        return HTMLResponse(jinja.get_template("routes.html").render(_ctx(
            request, active="routes",
            amount=amount, decision=decision, error=error,
        )))

    # Lightning capability per exchange — drives whether the network
    # selector + LN destination is offered. Aligned with what each
    # adapter's withdraw_btc() will actually accept.
    _LIGHTNING_SUPPORT = {
        "okx": True,
        "binance": False,
        "bitoasis": False,
    }

    def _withdrawals_ctx_extra():
        cfg = _config()
        # ExchangesConfig is a Pydantic model with one field per supported
        # exchange ({okx, binance, bitoasis}). Iterate model fields rather
        # than treating it as a dict.
        ex_names = list(cfg.exchanges.model_fields.keys()) if cfg.exchanges else []
        # Surface each known exchange — even ones without saved policy —
        # so the user can opt in. Existing policies render with their
        # stored values; unconfigured exchanges show defaults.
        policies = []
        existing = (cfg.auto_withdraw.exchanges or {})
        for name in ex_names:
            p = existing.get(name)
            policies.append({
                "exchange": name,
                "supports_lightning": _LIGHTNING_SUPPORT.get(name, False),
                "enabled": (p.enabled if p else False),
                "destination": (p.destination if p else "") or "",
                "network": (p.network if p else "bitcoin"),
                "threshold_btc": str(p.threshold_btc if p else Decimal("0.001")),
            })
        return {"policies": policies}

    @app.get("/withdrawals", response_class=HTMLResponse)
    async def withdrawals_page(request: Request):
        return HTMLResponse(jinja.get_template("withdrawals.html").render(_ctx(
            request, active="withdrawals", **_withdrawals_ctx_extra(),
        )))

    @app.post("/withdrawals", response_class=HTMLResponse)
    async def withdrawals_save(request: Request):
        form = await request.form()
        cfg = _config()
        ex_names = list(cfg.exchanges.model_fields.keys()) if cfg.exchanges else []

        # Auto-detect Lightning from the destination so a user who pastes
        # an `lnbc...` invoice doesn't have to also toggle the network.
        from bitcoiners_dca.core.lightning import (
            detect_network as _detect_network,
            is_lightning as _is_ln,
            WithdrawalNetwork as _Net,
        )

        def _refuse(msg: str):
            return HTMLResponse(jinja.get_template("withdrawals.html").render(_ctx(
                request, active="withdrawals", **_withdrawals_ctx_extra(),
                flash={"kind": "err", "message": msg},
            )))

        per_exchange: dict[str, dict] = {}
        any_enabled = False
        for name in ex_names:
            prefix = f"ex_{name}_"
            enabled = form.get(prefix + "enabled") == "on"
            destination = (form.get(prefix + "destination") or "").strip() or None
            requested_network = (form.get(prefix + "network") or "bitcoin").strip()
            try:
                threshold = Decimal(str(form.get(prefix + "threshold_btc", "0.001")).strip() or "0.001")
            except InvalidOperation:
                threshold = Decimal("0.001")

            # Auto-flip to lightning if the address is clearly an LN invoice/address.
            if destination and _is_ln(destination):
                requested_network = "lightning"

            # Validation runs only when the row is enabled — disabled rows
            # can have blank/garbage destination, we don't care.
            if enabled and destination:
                net = _detect_network(destination)
                if net == _Net.UNKNOWN:
                    return _refuse(
                        f"{name}: destination {destination[:40]!r} doesn't look like a "
                        "valid BTC address (bc1…/1…/3…) or Lightning address "
                        "(you@walletprovider.com). Double-check it before saving."
                    )
                # BOLT11 invoices expire (default 1h, max 7d). They make no
                # sense as an auto-withdraw destination — by the time the
                # bot has accumulated enough BTC to trip the threshold, the
                # invoice is dead. Refuse explicitly with a clear message
                # pointing at the alternatives.
                if net == _Net.LIGHTNING:
                    return _refuse(
                        f"{name}: that's a one-shot BOLT11 invoice — it'll "
                        "expire before the bot can reuse it. For ongoing "
                        "auto-withdraw, paste a static Lightning Address "
                        "(e.g. you@walletofsatoshi.com) or an on-chain BTC "
                        "address. Use the CLI for one-off invoice withdraws."
                    )
                # LNURL is opaque — we'd have to fetch + parse the response
                # to know what we're sending to. Same problem class as
                # BOLT11; reject.
                if net == _Net.LNURL:
                    return _refuse(
                        f"{name}: LNURL-pay destinations aren't supported "
                        "for auto-withdraw yet. Paste a Lightning Address "
                        "(you@host) or an on-chain BTC address instead."
                    )

            # Refuse LN on an exchange that doesn't support it — fail loudly
            # in the dashboard rather than silently dropping to on-chain.
            if (
                enabled
                and requested_network == "lightning"
                and not _LIGHTNING_SUPPORT.get(name, False)
            ):
                return _refuse(
                    f"{name} doesn't support Lightning withdrawals. Use an "
                    "on-chain BTC address or pick a different exchange."
                )

            per_exchange[name] = {
                "enabled": enabled,
                "destination": destination,
                "network": requested_network,
                "threshold_btc": str(threshold),
            }
            any_enabled = any_enabled or enabled

        patch = {
            "auto_withdraw.enabled": any_enabled,
            "auto_withdraw.exchanges": per_exchange,
        }
        flash = _apply_patch(state["config_path"], patch, _refresh_config)
        return HTMLResponse(jinja.get_template("withdrawals.html").render(_ctx(
            request, active="withdrawals", flash=flash, **_withdrawals_ctx_extra(),
        )))

    def _backtest_default_form(cfg) -> dict:
        # Pre-fill from live strategy config. Falls through to plain defaults
        # when fields are missing (e.g. very-new tenant with budget unset).
        from bitcoiners_dca.core.strategy import derive_per_cycle
        try:
            per_cycle = derive_per_cycle(
                Decimal(str(cfg.strategy.budget_amount or 500)),
                cfg.strategy.budget_period or "monthly",
                cfg.strategy.frequency or "weekly",
                getattr(cfg.strategy, "every_n_hours", None) or 1,
            )
        except Exception:
            per_cycle = Decimal("500")
        return {
            "days": 365,
            "amount_aed": str(int(per_cycle)),
            "frequency": cfg.strategy.frequency if cfg.strategy.frequency in ("daily","weekly","monthly") else "weekly",
            "day_of_week": (cfg.strategy.day_of_week if isinstance(cfg.strategy.day_of_week, int) else 0),
            "taker_fee_pct": "0.005",
            "dip_overlay": bool(getattr(cfg.overlays.buy_the_dip, "enabled", False)),
            "dip_threshold_pct": str(getattr(cfg.overlays.buy_the_dip, "threshold_pct", "-10")),
            "dip_multiplier": str(getattr(cfg.overlays.buy_the_dip, "multiplier", "2.0")),
        }

    @app.get("/backtest", response_class=HTMLResponse)
    async def backtest_page(request: Request):
        cfg = _config()
        return HTMLResponse(jinja.get_template("backtest.html").render(_ctx(
            request, active="backtest",
            form=_backtest_default_form(cfg),
            result=None, baseline=None, recent_cycles=None, error=None,
        )))

    @app.post("/backtest", response_class=HTMLResponse)
    async def backtest_run(request: Request):
        # Backtest is read-only — fetch historical prices, simulate, render.
        # No DB writes, no exchange calls. Safe to run repeatedly.
        from bitcoiners_dca.core.backtest import (
            BacktestConfig, naive_baseline, run_backtest,
        )
        from bitcoiners_dca.core.historical_prices import (
            HistoricalPriceSource, HistoricalPricesError,
        )
        form_raw = await request.form()
        # Coerce; surface any parse error to the user instead of 500ing.
        try:
            form = {
                "days": max(1, min(365, int(form_raw.get("days", "365")))),
                "amount_aed": str(form_raw.get("amount_aed", "500")),
                "frequency": str(form_raw.get("frequency", "weekly")),
                "day_of_week": int(form_raw.get("day_of_week", "0")),
                "taker_fee_pct": str(form_raw.get("taker_fee_pct", "0.005")),
                "dip_overlay": form_raw.get("dip_overlay") in ("on", "true", "1"),
                "dip_threshold_pct": str(form_raw.get("dip_threshold_pct", "-10")),
                "dip_multiplier": str(form_raw.get("dip_multiplier", "2.0")),
            }
            cfg = BacktestConfig(
                base_amount_aed=Decimal(form["amount_aed"]),
                frequency=form["frequency"],
                day_of_week=form["day_of_week"],
                taker_fee_pct=Decimal(form["taker_fee_pct"]),
                dip_overlay_enabled=form["dip_overlay"],
                dip_threshold_pct=Decimal(form["dip_threshold_pct"]),
                dip_multiplier=Decimal(form["dip_multiplier"]),
            )
        except (ValueError, InvalidOperation) as e:
            return HTMLResponse(jinja.get_template("backtest.html").render(_ctx(
                request, active="backtest",
                form=_backtest_default_form(_config()),
                result=None, baseline=None, recent_cycles=None,
                error=f"Invalid input: {e}",
            )))

        try:
            source = HistoricalPriceSource()
            points = source.fetch(vs_currency="aed", days=form["days"])
        except HistoricalPricesError as e:
            return HTMLResponse(jinja.get_template("backtest.html").render(_ctx(
                request, active="backtest", form=form,
                result=None, baseline=None, recent_cycles=None,
                error=str(e),
            )))

        # Try the hosted Pro API first if configured + the user has a Pro
        # license. Any failure (network, 4xx/5xx, stub:true) falls back
        # silently to the local engine — same pattern as router.pick().
        remote_result = await _remote_backtest(_license().key, cfg, points)
        result = remote_result if remote_result is not None else run_backtest(cfg, points)
        baseline = naive_baseline(cfg, points) if form["dip_overlay"] else None
        # Show last 30 cycles in the recent table; full history available
        # via the CLI's --show-cycles flag.
        recent = result.cycles[-30:] if result.cycles else []

        return HTMLResponse(jinja.get_template("backtest.html").render(_ctx(
            request, active="backtest", form=form,
            result=result, baseline=baseline, recent_cycles=recent, error=None,
        )))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return HTMLResponse(jinja.get_template("settings.html").render(_ctx(
            request, active="settings",
            license_features=[f.value for f in _license().enabled_features],
        )))

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = await request.form()
        patch = {
            "license.tier": form.get("license_tier", "free"),
            "license.key": form.get("license_key") or None,
            "notifications.telegram.enabled": form.get("tg_enabled") == "on",
            "notifications.telegram.chat_id":
                form.get("tg_chat_id") or None,
            "funding_monitor.enabled": form.get("funding_enabled") == "on",
            "funding_monitor.alert_threshold_pct":
                str(form.get("funding_threshold", "15.0")),
            "dry_run": form.get("dry_run") == "on",
        }
        flash = _apply_patch(state["config_path"], patch, _refresh_config)
        return HTMLResponse(jinja.get_template("settings.html").render(_ctx(
            request, active="settings", flash=flash,
            license_features=[f.value for f in _license().enabled_features],
        )))

    # === Lifecycle controls ===

    @app.post("/controls/pause", response_class=HTMLResponse)
    async def controls_pause(request: Request):
        """Manual pause via the dashboard. Skips every cycle until resumed."""
        from bitcoiners_dca.core.risk import RiskManager
        rm = RiskManager(_db(), max_consecutive_failures=999)
        rm.pause("manual via dashboard")
        return _redirect(request, "/")

    @app.post("/controls/resume", response_class=HTMLResponse)
    async def controls_resume(request: Request):
        from bitcoiners_dca.core.risk import RiskManager
        rm = RiskManager(_db(), max_consecutive_failures=999)
        rm.resume()
        return _redirect(request, "/")

    @app.post("/controls/go-live", response_class=HTMLResponse)
    async def controls_go_live(request: Request):
        """Flip dry_run=false. Cycle next time cron fires."""
        _apply_patch(state["config_path"], {"dry_run": False}, _refresh_config)
        return _redirect(request, "/")

    @app.post("/controls/go-dry-run", response_class=HTMLResponse)
    async def controls_go_dry_run(request: Request):
        """Flip dry_run=true. Cycles still tick but no real orders placed."""
        _apply_patch(state["config_path"], {"dry_run": True}, _refresh_config)
        return _redirect(request, "/")

    @app.post("/controls/buy-now", response_class=HTMLResponse)
    async def controls_buy_now(request: Request):
        """Trigger an immediate one-shot DCA cycle, regardless of cron
        schedule.

        Respects RiskManager caps (daily + single-buy) and pause state.
        Previously bypassed them entirely — a customer could blow through
        their AED daily cap with rapid Buy-now clicks. Now if risk caps
        would block the cycle, we redirect with a clear error.
        """
        from bitcoiners_dca.cli import _buy_once
        from bitcoiners_dca.core.risk import RiskManager
        cfg = _config()
        rm = RiskManager(
            _db(),
            max_consecutive_failures=cfg.risk.max_consecutive_failures,
            max_daily_aed=cfg.risk.max_daily_aed,
            max_single_buy_aed=cfg.risk.max_single_buy_aed,
            timezone_str=cfg.strategy.timezone or "Asia/Dubai",
        )
        # Use the strategy's intended per-cycle amount for the check.
        decision = rm.evaluate(Decimal(str(cfg.strategy.amount_aed)))
        if not decision.allow:
            return _redirect(
                request,
                f"/trades?error=Buy now blocked by risk caps: {'; '.join(decision.reasons)[:200]}",
            )
        try:
            await _buy_once(state["config_path"], dry=False)
        except Exception as e:
            return _redirect(request, f"/trades?error={str(e)[:200]}")
        # _buy_once swallows cycle-internal errors and records them in
        # cycle_log. If the most recent cycle has errors and no order,
        # report that to the user instead of pretending success.
        try:
            row = _db()._conn.execute(
                "SELECT success, errors FROM cycle_log "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row and not row["success"]:
                import json as _json
                errs = _json.loads(row["errors"] or "[]")
                err_msg = "; ".join(errs)[:300] or "cycle returned no order"
                return _redirect(request, f"/trades?error=Buy now did not execute: {err_msg}")
        except Exception:
            pass
        return _redirect(request, "/trades?ran=1")

    # === JSON endpoints (kept for backward compat + CLI/scripting use) ===

    @app.get("/api/stats")
    async def api_stats():
        db = _db()
        total_btc = db.total_btc_bought()
        total_aed = db.total_aed_spent()
        return {
            "total_btc": str(total_btc),
            "total_aed_spent": str(total_aed),
            "average_cost_per_btc":
                str(total_aed / total_btc) if total_btc > 0 else "0",
            "trades_count": db._conn.execute(
                "SELECT COUNT(*) FROM trades"
            ).fetchone()[0],
        }

    @app.get("/api/balances")
    async def api_balances():
        results = await asyncio.gather(
            *[_safe_get_balances(ex) for ex in _exchanges()]
        )
        return dict(results)

    @app.get("/api/prices")
    async def api_prices(pair: str = "BTC/AED"):
        results = await asyncio.gather(
            *[_safe_get_ticker(ex, pair) for ex in _exchanges()]
        )
        return dict(results)

    @app.get("/api/current-btc-aed")
    async def api_current_btc_aed():
        """Cheapest current BTC/AED ask across enabled exchanges, for
        the strategy form's live preview. Cached 45s via the same TTL
        cache as /htmx/prices so a customer typing in the budget field
        doesn't hammer exchange APIs.
        """
        import time as _t
        entry = _live_cache.get("current_btc_aed")
        if entry and _t.time() - entry[0] <= _LIVE_CACHE_TTL:
            return {"price_aed": entry[1], "cached": True}
        results = await asyncio.gather(
            *[_safe_get_ticker(ex, "BTC/AED") for ex in _exchanges()],
            return_exceptions=True,
        )
        prices = []
        for r in results:
            if isinstance(r, Exception):
                continue
            _name, t = r
            ask = t.get("ask")
            if ask:
                prices.append(float(ask))
        price = min(prices) if prices else None
        if price:
            _cache_set("current_btc_aed", price)
        return {"price_aed": price, "cached": False}

    @app.get("/api/cumulative-btc")
    async def api_cumulative_btc():
        db = _db()
        rows = db._conn.execute(
            """SELECT substr(timestamp, 1, 10) AS day,
                      SUM(CAST(amount_base AS REAL)) AS btc_for_day
               FROM trades
               WHERE side='buy' AND status='filled'
               GROUP BY day ORDER BY day ASC"""
        ).fetchall()
        out = []
        cum = 0.0
        for r in rows:
            cum += float(r["btc_for_day"] or 0)
            out.append({"date": r["day"], "cumulative_btc": f"{cum:.8f}"})
        return {"points": out}

    @app.get("/api/cost-basis-vs-market")
    async def api_cost_basis_vs_market():
        db = _db()
        rows = db._conn.execute(
            """SELECT substr(timestamp, 1, 10) AS day,
                      SUM(CAST(amount_quote AS REAL)) AS aed,
                      SUM(CAST(amount_base AS REAL)) AS btc
               FROM trades
               WHERE side='buy' AND status='filled'
               GROUP BY day ORDER BY day ASC"""
        ).fetchall()
        out = []
        cum_aed = 0.0
        cum_btc = 0.0
        for r in rows:
            cum_aed += float(r["aed"] or 0)
            cum_btc += float(r["btc"] or 0)
            avg = (cum_aed / cum_btc) if cum_btc > 0 else 0.0
            out.append({"date": r["day"], "avg_cost_aed_per_btc": f"{avg:.2f}"})
        current_market = None
        for ex in _exchanges():
            try:
                t = await ex.get_ticker("BTC/AED")
                current_market = f"{float(t.last):.2f}"
                break
            except Exception:
                continue
        return {"points": out, "current_market_aed_per_btc": current_market}

    @app.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "now": datetime.utcnow().isoformat(),
            "exchanges_configured": [ex.name for ex in _exchanges()],
        }

    return app


# === Helpers ===

def _apply_patch(cfg_path: str | Path, patch: dict, refresh) -> dict:
    """Apply config patch + refresh in-memory state. Returns a flash dict.

    `cfg_path` is the path to the tenant's config.yaml — provided by the
    caller so the writer touches the right file. Hardcoding `./config.yaml`
    here used to silently write to /app/config.yaml inside the tenant
    container, which doesn't exist (per-tenant config is at /app/config/
    config.yaml), and every Save returned a misleading error.
    """
    writer = ConfigWriter(Path(cfg_path))
    try:
        result = writer.patch_and_save(patch)
        refresh()
        if not result.changed_keys:
            return {"kind": "warn", "message": "No changes detected."}
        return {
            "kind": "ok",
            "message": f"Saved. Changes will apply on the next cycle. "
                       f"({len(result.changed_keys)} fields updated)",
        }
    except ConfigWriteError as e:
        return {"kind": "err", "message": f"Validation failed: {e}"}
    except Exception as e:
        return {"kind": "err", "message": f"Write failed: {e}"}


def _redact(value: str) -> str:
    """Bullets only — never leak prefix/suffix of stored secrets to the DOM.
    Pairs with `persistence.secrets._redact` (same semantics)."""
    if not value:
        return ""
    return "••••••••"


def _parse_risk_cap(raw) -> Optional[str]:
    """Risk-cap form input → YAML value. "" / "0" / "0.0" → None (no cap).
    Real positive number → preserved as string. Anything else → None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        v = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if v <= Decimal(0):
        return None
    return str(v)


def _build_dashboard_exchanges(
    cfg: AppConfig,
    secrets: Optional[SecretStore],
) -> list[Exchange]:
    """Construct exchange adapters using SecretStore-backed credentials, with
    env-var fallback. Mirrors `cli._build_exchanges()` but plumbs through
    the secret store for the customer-managed credentials path."""
    out: list[Exchange] = []
    if cfg.exchanges.okx.enabled:
        creds = _resolve_creds(secrets, "okx", {
            "api_key": cfg.exchanges.okx.api_key_env or "OKX_API_KEY",
            "api_secret": cfg.exchanges.okx.api_secret_env or "OKX_API_SECRET",
            "passphrase": cfg.exchanges.okx.passphrase_env or "OKX_API_PASSPHRASE",
        })
        if creds.get("api_key"):
            from bitcoiners_dca.exchanges.okx import OKXExchange
            out.append(OKXExchange(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                passphrase=creds.get("passphrase", ""),
                dry_run=cfg.dry_run,
            ))
    if cfg.exchanges.binance.enabled:
        creds = _resolve_creds(secrets, "binance", {
            "api_key": cfg.exchanges.binance.api_key_env or "BINANCE_API_KEY",
            "api_secret": cfg.exchanges.binance.api_secret_env or "BINANCE_API_SECRET",
        })
        if creds.get("api_key"):
            from bitcoiners_dca.exchanges.binance import BinanceExchange
            out.append(BinanceExchange(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                dry_run=cfg.dry_run,
            ))
    if cfg.exchanges.bitoasis.enabled:
        creds = _resolve_creds(secrets, "bitoasis", {
            "token": cfg.exchanges.bitoasis.token_env or "BITOASIS_API_TOKEN",
        })
        if creds.get("token"):
            from bitcoiners_dca.exchanges.bitoasis import BitOasisExchange
            out.append(BitOasisExchange(
                api_token=creds["token"],
                dry_run=cfg.dry_run,
            ))
    return out


def _resolve_creds(
    secrets: Optional[SecretStore],
    exchange: str,
    env_var_map: dict[str, str],
) -> dict[str, str]:
    """SecretStore first, env var fallback. Returns whatever fields resolved."""
    out: dict[str, str] = {}
    if secrets is not None:
        out.update(credentials_for(secrets, exchange))
    # env-var fallback for fields not in the secret store
    for field, env_name in env_var_map.items():
        if field not in out:
            val = os.environ.get(env_name)
            if val:
                out[field] = val
    return out


async def _safe_get_balances(ex: Exchange) -> tuple[str, list[dict] | dict]:
    try:
        bals = await ex.get_balances()
        return ex.name, [
            {"asset": b.asset, "free": str(b.free),
             "used": str(b.used), "total": str(b.total)}
            for b in bals
        ]
    except Exception as e:
        return ex.name, {"error": str(e)[:200]}


async def _safe_get_ticker(ex: Exchange, pair: str) -> tuple[str, dict]:
    try:
        t = await ex.get_ticker(pair)
        return ex.name, {
            "bid": str(t.bid), "ask": str(t.ask), "last": str(t.last),
            "spread_pct": f"{float(t.spread_pct):.4f}",
        }
    except Exception as e:
        return ex.name, {"error": str(e)[:200]}


# === Module-level app for `uvicorn bitcoiners_dca.web.dashboard:app` ===

app = create_app()
