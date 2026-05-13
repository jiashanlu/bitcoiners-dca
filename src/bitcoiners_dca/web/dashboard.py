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

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        db = _db()
        cfg = _config()
        total_btc = db.total_btc_bought()
        total_aed = db.total_aed_spent()
        avg_price = total_aed / total_btc if total_btc > 0 else Decimal(0)
        cur = db._conn.execute(
            """SELECT timestamp, exchange, pair, amount_quote, amount_base,
                      price_avg, order_id
               FROM trades
               WHERE side='buy' AND status='filled'
               ORDER BY timestamp DESC LIMIT 8"""
        )
        recent = cur.fetchall()
        arb_count = db._conn.execute(
            "SELECT COUNT(*) FROM arbitrage_log WHERE alerted=1"
        ).fetchone()[0]
        cycle_count = db._conn.execute(
            "SELECT COUNT(*) FROM cycle_log"
        ).fetchone()[0]
        return HTMLResponse(jinja.get_template("overview.html").render(_ctx(
            request, active="overview",
            total_btc=total_btc, total_aed=total_aed, avg_price=avg_price,
            recent=recent, arb_count=arb_count, cycle_count=cycle_count,
        )))

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
        return HTMLResponse(jinja.get_template("exchanges.html").render(_ctx(
            request, active="exchanges",
            credentials=creds, secrets_available=sec is not None,
            required_fields=required_fields,
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

    @app.get("/htmx/balances", response_class=HTMLResponse)
    async def htmx_balances(request: Request):
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

        # Decorate rows: localized timestamp + grouped route label for
        # multi-hop cycles. Two trades < 5 seconds apart on the same
        # exchange are treated as legs of the same cycle. We reconstruct
        # the route by chaining their pairs (e.g. AED→USDT→BTC).
        from datetime import datetime, timezone as _tz
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(user_tz)
        except Exception:
            tz = ZoneInfo("Asia/Dubai")

        # Sort ascending so route grouping reads naturally, then we'll
        # reverse at the end for display.
        sorted_asc = sorted(
            [dict(r) for r in rows], key=lambda r: r["timestamp"]
        )
        decorated: list[dict] = []
        cur_group: list[dict] = []

        def _parse(ts: str) -> datetime:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt

        def _close_group(group: list[dict]):
            if not group:
                return
            pair_legs = [g["pair"] for g in group]
            # Render route as A→B→C from "X/Y" pairs (X is bought, Y is
            # spent). Read each pair as "buy-X-with-Y" then chain.
            currencies: list[str] = []
            for p in pair_legs:
                base, quote = (p.split("/") + [""])[:2]
                if not currencies:
                    currencies.append(quote)
                currencies.append(base)
            route = " → ".join(currencies) if len(currencies) > 1 else None
            for g in group:
                g["route"] = route if len(group) > 1 else None
                g["is_leg"] = len(group) > 1
            decorated.extend(group)

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
        # Strip the helper datetime; not JSON-serialisable in some Jinja
        # filters and not needed by the template.
        for r in decorated:
            r.pop("ts_dt", None)
        decorated.reverse()

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
            )
        except Exception as e:
            error = str(e)[:200]
        return HTMLResponse(jinja.get_template("routes.html").render(_ctx(
            request, active="routes",
            amount=amount, decision=decision, error=error,
        )))

    @app.get("/withdrawals", response_class=HTMLResponse)
    async def withdrawals_page(request: Request):
        return HTMLResponse(jinja.get_template("withdrawals.html").render(_ctx(
            request, active="withdrawals",
        )))

    @app.post("/withdrawals", response_class=HTMLResponse)
    async def withdrawals_save(request: Request):
        form = await request.form()
        # Address validation: empty if disabled is fine; otherwise require
        # something that looks like a BTC address. Light client-side regex
        # is the form's job; here we just bound the field shape.
        addr = (form.get("destination_address") or "").strip() or None
        try:
            thr = Decimal(str(form.get("threshold_btc", "0.01")).strip() or "0.01")
        except InvalidOperation:
            thr = Decimal("0.01")
        patch = {
            "auto_withdraw.enabled": form.get("enabled") == "on",
            "auto_withdraw.destination_address": addr,
            "auto_withdraw.threshold_btc": str(thr),
        }
        flash = _apply_patch(state["config_path"], patch, _refresh_config)
        return HTMLResponse(jinja.get_template("withdrawals.html").render(_ctx(
            request, active="withdrawals", flash=flash,
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
