"""
Read-only FastAPI dashboard.

The dashboard intentionally does NOT expose any mutation endpoints — no buy
buttons, no key rotation, no withdraw triggers. It's a window into the bot's
state. Users interact with the bot via the CLI or by editing config.yaml.

Routes:
  GET /                  — overview (recent trades + balances + next-cycle ETA)
  GET /trades            — full trade history (paginated)
  GET /arbitrage         — arbitrage opportunities log
  GET /api/stats         — JSON: lifetime totals
  GET /api/balances      — JSON: current exchange balances
  GET /api/prices        — JSON: current ticker across exchanges
  GET /healthz           — JSON: scheduler + exchange health

Run with:
  uvicorn bitcoiners_dca.web.dashboard:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from bitcoiners_dca.exchanges.base import Exchange
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.utils.config import AppConfig, load_config


# === FACTORY ===

def create_app(
    config: Optional[AppConfig] = None,
    db: Optional[Database] = None,
    exchanges: Optional[list[Exchange]] = None,
) -> FastAPI:
    """Build a FastAPI app with the given dependencies injected.

    When run via uvicorn standalone, dependencies are lazily loaded from
    config.yaml on first request.
    """
    app = FastAPI(
        title="bitcoiners-dca dashboard",
        description="Read-only operations dashboard.",
        version="0.1.0",
    )

    # Lazy-load on first access; cached after
    state: dict = {"config": config, "db": db, "exchanges": exchanges}

    def _config() -> AppConfig:
        if state["config"] is None:
            state["config"] = load_config()
        return state["config"]

    def _db() -> Database:
        if state["db"] is None:
            state["db"] = Database(_config().persistence.db_path)
        return state["db"]

    def _exchanges() -> list[Exchange]:
        if state["exchanges"] is None:
            from bitcoiners_dca.cli import _build_exchanges
            state["exchanges"] = _build_exchanges(_config())
        return state["exchanges"]

    # === ROUTES ===

    @app.get("/", response_class=HTMLResponse)
    async def index():
        db = _db()
        total_btc = db.total_btc_bought()
        total_aed = db.total_aed_spent()
        avg_price = total_aed / total_btc if total_btc > 0 else Decimal(0)

        cur = db._conn.execute(
            """SELECT timestamp, exchange, pair, side, amount_quote,
                      amount_base, price_avg, order_id
               FROM trades
               WHERE side='buy' AND status='filled'
               ORDER BY timestamp DESC
               LIMIT 10"""
        )
        recent = cur.fetchall()

        arb_count = db._conn.execute(
            "SELECT COUNT(*) FROM arbitrage_log WHERE alerted=1"
        ).fetchone()[0]

        rows_html = "".join(
            f"""<tr>
                <td>{r['timestamp'].split('T')[0]}</td>
                <td>{r['exchange']}</td>
                <td>{r['pair']}</td>
                <td>{float(r['amount_quote']):.2f}</td>
                <td>{float(r['amount_base'] or 0):.8f}</td>
                <td>{float(r['price_avg'] or 0):.2f}</td>
            </tr>"""
            for r in recent
        )
        if not rows_html:
            rows_html = "<tr><td colspan='6' style='text-align:center;color:#888'>No trades yet</td></tr>"

        return HTMLResponse(
            f"""<!doctype html>
<html><head>
<title>bitcoiners-dca dashboard</title>
<style>
body{{font-family:-apple-system,Inter,sans-serif;background:#0a0a0a;color:#f5f5f5;margin:0;padding:32px;max-width:1100px;margin:0 auto}}
h1{{font-size:36px;letter-spacing:-0.03em;margin-bottom:8px}}
h2{{font-size:22px;letter-spacing:-0.02em;margin-top:48px;color:#F7931A}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:24px 0}}
.stat{{background:#111;border:1px solid #222;border-radius:10px;padding:20px}}
.stat .label{{color:#888;font-size:13px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}}
.stat .val{{font-size:28px;font-weight:800;letter-spacing:-0.02em;color:#fff}}
.stat .unit{{font-size:14px;color:#888;margin-left:4px}}
table{{width:100%;border-collapse:collapse;margin-top:16px;font-size:14px}}
th{{text-align:left;padding:10px;color:#888;border-bottom:1px solid #222;text-transform:uppercase;font-size:11px;letter-spacing:1px}}
td{{padding:10px;border-bottom:1px solid #181818;color:#f5f5f5}}
a{{color:#F7931A;text-decoration:none}}
.nav{{display:flex;gap:18px;margin-top:8px;color:#888;font-size:14px}}
</style>
</head><body>
<h1>🟧 bitcoiners-dca</h1>
<div class="nav">
  <a href="/">Overview</a>
  <a href="/trades">Trades</a>
  <a href="/arbitrage">Arbitrage</a>
  <a href="/api/prices">Live prices (JSON)</a>
  <a href="/healthz">Health</a>
</div>

<div class="stats">
  <div class="stat"><div class="label">Total BTC</div><div class="val">{total_btc:.6f}<span class="unit">BTC</span></div></div>
  <div class="stat"><div class="label">Total AED spent</div><div class="val">{total_aed:.0f}<span class="unit">AED</span></div></div>
  <div class="stat"><div class="label">Avg cost basis</div><div class="val">{avg_price:.0f}<span class="unit">AED/BTC</span></div></div>
  <div class="stat"><div class="label">Arb opportunities</div><div class="val">{arb_count}</div></div>
</div>

<h2>BTC accumulation</h2>
<div style="background:#111;border:1px solid #222;border-radius:10px;padding:16px">
  <canvas id="cumChart" height="90"></canvas>
</div>

<h2>Avg cost basis vs market</h2>
<div style="background:#111;border:1px solid #222;border-radius:10px;padding:16px">
  <canvas id="costChart" height="90"></canvas>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" defer></script>
<script defer>
window.addEventListener('load', async () => {{
  if (typeof Chart === 'undefined') return;
  const dim = '#888'; const acc = '#F7931A';
  Chart.defaults.color = dim;
  Chart.defaults.borderColor = '#222';
  try {{
    const cum = await fetch('/api/cumulative-btc').then(r => r.json());
    new Chart(document.getElementById('cumChart'), {{
      type: 'line',
      data: {{
        labels: cum.points.map(p => p.date),
        datasets: [{{
          label: 'Cumulative BTC',
          data: cum.points.map(p => parseFloat(p.cumulative_btc)),
          borderColor: acc,
          backgroundColor: 'rgba(247,147,26,0.1)',
          fill: true, tension: 0.2, pointRadius: 0,
        }}],
      }},
      options: {{
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ y: {{ beginAtZero: true }} }},
      }},
    }});
  }} catch (e) {{}}
  try {{
    const cost = await fetch('/api/cost-basis-vs-market').then(r => r.json());
    const datasets = [{{
      label: 'Avg cost basis',
      data: cost.points.map(p => parseFloat(p.avg_cost_aed_per_btc)),
      borderColor: '#7aa9ff',
      backgroundColor: 'rgba(122,169,255,0.08)',
      fill: false, tension: 0.2, pointRadius: 0,
    }}];
    if (cost.current_market_aed_per_btc) {{
      datasets.push({{
        label: 'Market now',
        data: cost.points.map(() => parseFloat(cost.current_market_aed_per_btc)),
        borderColor: '#5cbb84', borderDash: [4, 4],
        pointRadius: 0, fill: false,
      }});
    }}
    new Chart(document.getElementById('costChart'), {{
      type: 'line',
      data: {{ labels: cost.points.map(p => p.date), datasets }},
      options: {{
        plugins: {{ legend: {{ display: true }} }},
        scales: {{ y: {{ beginAtZero: false }} }},
      }},
    }});
  }} catch (e) {{}}
}});
</script>

<h2>Recent trades</h2>
<table>
  <thead><tr>
    <th>Date</th><th>Exchange</th><th>Pair</th>
    <th>AED</th><th>BTC</th><th>Price</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</body></html>"""
        )

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(page: int = Query(1, ge=1), per_page: int = 50):
        db = _db()
        offset = (page - 1) * per_page
        cur = db._conn.execute(
            """SELECT * FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            (per_page, offset),
        )
        rows = cur.fetchall()
        total = db._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        rows_html = "".join(
            f"""<tr>
                <td>{r['timestamp'].split('T')[0]}</td>
                <td>{r['exchange']}</td>
                <td>{r['side']}</td>
                <td>{float(r['amount_quote']):.2f}</td>
                <td>{float(r['amount_base'] or 0):.8f}</td>
                <td>{float(r['price_avg'] or 0):.2f}</td>
                <td>{r['status']}</td>
                <td><code style='font-size:11px;color:#888'>{r['order_id'][:16]}</code></td>
            </tr>"""
            for r in rows
        )

        return HTMLResponse(
            f"""<!doctype html><html><head>
<title>Trades · bitcoiners-dca</title>
<style>body{{font-family:-apple-system,Inter,sans-serif;background:#0a0a0a;color:#f5f5f5;padding:32px;max-width:1100px;margin:0 auto}}h1{{font-size:28px}}a{{color:#F7931A}}table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:16px}}th{{text-align:left;padding:10px;color:#888;border-bottom:1px solid #222;font-size:11px;text-transform:uppercase}}td{{padding:10px;border-bottom:1px solid #181818}}.nav{{margin-bottom:16px}}</style>
</head><body>
<div class="nav"><a href="/">← Back to overview</a></div>
<h1>Trades ({total} total)</h1>
<table>
  <thead><tr><th>Date</th><th>Exchange</th><th>Side</th><th>AED</th><th>BTC</th><th>Price</th><th>Status</th><th>Order ID</th></tr></thead>
  <tbody>{rows_html or "<tr><td colspan='8' style='text-align:center;color:#888'>No trades yet</td></tr>"}</tbody>
</table>
<p style="margin-top:24px;color:#888;font-size:13px">
  Page {page} · {per_page} per page ·
  {f'<a href="/trades?page={page-1}&per_page={per_page}">← Prev</a> ' if page > 1 else ''}
  {f'<a href="/trades?page={page+1}&per_page={per_page}">Next →</a>' if offset + per_page < total else ''}
</p>
</body></html>"""
        )

    @app.get("/arbitrage", response_class=HTMLResponse)
    async def arbitrage_page():
        db = _db()
        cur = db._conn.execute(
            """SELECT * FROM arbitrage_log ORDER BY timestamp DESC LIMIT 100"""
        )
        rows = cur.fetchall()

        rows_html = "".join(
            f"""<tr>
                <td>{r['timestamp']}</td>
                <td>{r['cheap_exchange']}</td>
                <td>{r['expensive_exchange']}</td>
                <td>{float(r['gross_spread_pct']):.2f}%</td>
                <td style='color:#F7931A;font-weight:600'>{float(r['net_profit_pct']):.2f}%</td>
            </tr>"""
            for r in rows
        )

        return HTMLResponse(
            f"""<!doctype html><html><head>
<title>Arbitrage · bitcoiners-dca</title>
<style>body{{font-family:-apple-system,Inter,sans-serif;background:#0a0a0a;color:#f5f5f5;padding:32px;max-width:900px;margin:0 auto}}h1{{font-size:28px}}a{{color:#F7931A}}table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:16px}}th{{text-align:left;padding:10px;color:#888;border-bottom:1px solid #222;font-size:11px;text-transform:uppercase}}td{{padding:10px;border-bottom:1px solid #181818}}.nav{{margin-bottom:16px}}</style>
</head><body>
<div class="nav"><a href="/">← Back to overview</a></div>
<h1>Arbitrage opportunities (last 100)</h1>
<table>
  <thead><tr><th>Detected</th><th>Buy on</th><th>Sell on</th><th>Gross %</th><th>Net %</th></tr></thead>
  <tbody>{rows_html or "<tr><td colspan='5' style='text-align:center;color:#888'>No opportunities detected yet</td></tr>"}</tbody>
</table>
</body></html>"""
        )

    @app.get("/api/stats")
    async def api_stats():
        db = _db()
        total_btc = db.total_btc_bought()
        total_aed = db.total_aed_spent()
        return {
            "total_btc": str(total_btc),
            "total_aed_spent": str(total_aed),
            "average_cost_per_btc": (
                str(total_aed / total_btc) if total_btc > 0 else "0"
            ),
            "trades_count": db._conn.execute(
                "SELECT COUNT(*) FROM trades"
            ).fetchone()[0],
        }

    @app.get("/api/balances")
    async def api_balances():
        balances = {}
        tasks = []
        for ex in _exchanges():
            tasks.append(_safe_get_balances(ex))
        results = await asyncio.gather(*tasks)
        for name, bals in results:
            balances[name] = bals
        return balances

    @app.get("/api/prices")
    async def api_prices(pair: str = "BTC/AED"):
        tickers = {}
        tasks = []
        for ex in _exchanges():
            tasks.append(_safe_get_ticker(ex, pair))
        results = await asyncio.gather(*tasks)
        for name, t in results:
            tickers[name] = t
        return tickers

    @app.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "now": datetime.utcnow().isoformat(),
            "exchanges_configured": [ex.name for ex in _exchanges()],
        }

    @app.get("/api/cumulative-btc")
    async def api_cumulative_btc():
        """BTC stack over time — for the dashboard line chart.

        Returns rows like {"date": "2026-05-12", "cumulative_btc": "0.0823"}
        in chronological order. Skip-friendly for very long histories;
        we downsample to one point per day even if there were many trades.
        """
        db = _db()
        rows = db._conn.execute(
            """SELECT substr(timestamp, 1, 10) AS day,
                      SUM(CAST(amount_base AS REAL)) AS btc_for_day
               FROM trades
               WHERE side='buy' AND status='filled'
               GROUP BY day
               ORDER BY day ASC"""
        ).fetchall()
        out = []
        cumulative = 0.0
        for r in rows:
            cumulative += float(r["btc_for_day"] or 0)
            out.append({
                "date": r["day"],
                "cumulative_btc": f"{cumulative:.8f}",
            })
        return {"points": out}

    @app.get("/api/cost-basis-vs-market")
    async def api_cost_basis_vs_market():
        """Average cost basis over time vs current market price.

        Lets a user see "my avg buy price is X, BTC is now Y, so my stack
        is up/down Z%". We compute avg cost per BTC at each day's close
        plus today's market price for comparison.
        """
        db = _db()
        rows = db._conn.execute(
            """SELECT substr(timestamp, 1, 10) AS day,
                      SUM(CAST(amount_quote AS REAL)) AS aed,
                      SUM(CAST(amount_base AS REAL))  AS btc
               FROM trades
               WHERE side='buy' AND status='filled'
               GROUP BY day
               ORDER BY day ASC"""
        ).fetchall()
        out = []
        cum_aed = 0.0
        cum_btc = 0.0
        for r in rows:
            cum_aed += float(r["aed"] or 0)
            cum_btc += float(r["btc"] or 0)
            avg = (cum_aed / cum_btc) if cum_btc > 0 else 0.0
            out.append({
                "date": r["day"],
                "avg_cost_aed_per_btc": f"{avg:.2f}",
            })

        # Best-effort market price for today (first enabled exchange's ticker)
        current_market = None
        for ex in _exchanges():
            try:
                t = await ex.get_ticker("BTC/AED")
                current_market = f"{float(t.last):.2f}"
                break
            except Exception:
                continue
        return {
            "points": out,
            "current_market_aed_per_btc": current_market,
        }

    return app


# === SAFE HELPERS (don't blow up on one bad exchange) ===

async def _safe_get_balances(ex: Exchange) -> tuple[str, list[dict] | dict]:
    try:
        bals = await ex.get_balances()
        return ex.name, [b.model_dump(mode="json") for b in bals]
    except Exception as e:
        return ex.name, {"error": str(e)[:200]}


async def _safe_get_ticker(ex: Exchange, pair: str) -> tuple[str, dict]:
    try:
        t = await ex.get_ticker(pair)
        return ex.name, t.model_dump(mode="json")
    except Exception as e:
        return ex.name, {"error": str(e)[:200]}


# === MODULE-LEVEL APP (for `uvicorn bitcoiners_dca.web.dashboard:app`) ===

app = create_app()
