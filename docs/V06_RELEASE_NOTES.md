# v0.6 release notes — Customer dashboard

Shipped 2026-05-12. The bot is now a real self-service product.

## What's new

### Customer-facing dashboard

The dashboard is no longer just a read-only window — customers can now
configure everything from a browser.

| Page | What you can do |
|---|---|
| **Overview** | KPIs, accumulation + cost-basis charts, recent activity |
| **Strategy** | Cycle amount + frequency · execution mode · all overlays · routing knobs · risk caps |
| **Exchanges** | Enable/disable per exchange · paste API credentials (encrypted at rest, redacted on display) |
| **Balances** | Live across all enabled exchanges, refreshes every 30 s |
| **Prices** | Live BTC tickers, switchable pair (BTC/AED / BTC/USDT / USDT/AED), refreshes every 10 s |
| **Trades** | Paginated full history with status pills |
| **Routes audit** | Web version of the `routes` CLI — every viable path at a user-chosen size |
| **Settings** | License key entry · notifications · funding monitor · dry-run toggle |

### Encrypted secret storage

`persistence/secrets.py` — Fernet-encrypted blobs in SQLite, key from
`DCA_SECRETS_KEY` env var. Customers paste API credentials into the
Exchanges page; we store the ciphertext + display the redacted form
(`abc…ef4`). Plaintext only ever exists in memory inside the daemon
when constructing exchange clients.

For self-hosters who prefer env-var-based secrets, the bot still respects
them as a fallback when no SecretStore entry exists. Either workflow works.

### Atomic config writes

`web/config_writer.py` — Pydantic-validated patches applied to
`config.yaml` via temp-file + fsync + atomic rename. Bad patches are
caught BEFORE touching disk (so a customer can't brick their daemon by
saving an invalid form).

### Hot config reload

The scheduler now re-reads config + secrets at the start of every
scheduled task. Dashboard edits take effect on the next cycle — no
daemon restart needed.

### Cloudflare Access integration

The dashboard trusts the `Cf-Access-Authenticated-User-Email` header
that CF Access populates after gating the user. From inside the LAN
(direct dashboard access), no header → "local-operator". From outside,
CF Access does email OTP / IP bypass as configured. Same posture as
your `dev.bitcoiners.ae` dashboard.

### Repo went git

`bitcoiners-dca` is now its own git repo (`jiashan-dev/bitcoiners-dca`
on Gitea, private). Single 165-passing-test snapshot as the v0.5 baseline
commit; v0.6 work coming in follow-up commits.

## Architecture changes

```
                  Cloudflare Tunnel
                        │
                        ▼
                  Cloudflare Access
                        │
                        ▼
                  dca-dev.bitcoiners.ae
                        │
                        ▼
                  Mac mini :8000
                  ┌─────────────┐
                  │  dashboard  │ (FastAPI + Jinja + HTMX)
                  │             │ ├─ /strategy  ──▶ writes config.yaml
                  │             │ ├─ /exchanges ──▶ writes SecretStore
                  │             │ ├─ /balances  ──▶ live exchange queries
                  │             │ └─ /routes-audit ──▶ live router math
                  └──────┬──────┘
                         │ shares config.yaml + data/dca.db
                  ┌──────▼──────┐
                  │   daemon    │ (apscheduler)
                  │             │ ├─ re-reads config every cycle
                  │             │ ├─ reads secrets from SecretStore
                  │             │ └─ runs DCA + arbitrage + funding monitor
                  └─────────────┘
```

## Test count

- v0.5 → v0.6: **165 → 165** (no regressions; added test_secret_store.py
  and test_config_writer.py — 8 + 8 new tests covering the new infra)

## Files added

```
src/bitcoiners_dca/persistence/secrets.py           encrypted-at-rest creds
src/bitcoiners_dca/web/config_writer.py             atomic YAML edits
src/bitcoiners_dca/web/jinja_env.py                 Jinja2 setup
src/bitcoiners_dca/web/templates/_base.html         shared layout + nav
src/bitcoiners_dca/web/templates/overview.html
src/bitcoiners_dca/web/templates/strategy.html
src/bitcoiners_dca/web/templates/exchanges.html
src/bitcoiners_dca/web/templates/balances.html
src/bitcoiners_dca/web/templates/prices.html
src/bitcoiners_dca/web/templates/trades.html
src/bitcoiners_dca/web/templates/routes.html
src/bitcoiners_dca/web/templates/settings.html
src/bitcoiners_dca/web/templates/partials/balances_table.html
src/bitcoiners_dca/web/templates/partials/prices_table.html
tests/test_secret_store.py
tests/test_config_writer.py
docs/V06_RELEASE_NOTES.md
```

## Files changed

- `src/bitcoiners_dca/web/dashboard.py` — full rewrite, ~13 → ~30 routes,
  Jinja-rendered pages + HTMX partials + form POST handlers
- `src/bitcoiners_dca/cli.py` — `_build_exchanges` now resolves creds via
  SecretStore first, env vars as fallback; `run` command wires a hot-
  reload factory into the scheduler
- `src/bitcoiners_dca/core/scheduler.py` — `_reload_if_changed()` at the
  top of every scheduled task picks up dashboard config edits
- `pyproject.toml` — version → 0.6.0; added jinja2 + python-multipart +
  cryptography to runtime deps; added `[tool.setuptools.package-data]` so
  templates ship inside the wheel
- `docker-compose.yml` — explicit `image: bitcoiners-dca:latest`; dashboard
  data mount is now read-write (needed for SecretStore CRUD)

## Things deliberately deferred

- **Multi-tenant within a single container.** Current design = one
  container per customer (via `hosted/provision.sh`). Simpler isolation,
  easier billing, no shared-DB authorization complexity. We add per-
  tenant routing in nginx, not in the bot.
- **Real-time balance push via WebSocket.** HTMX polling at 30 s is
  fine for DCA-cadence operation; WebSockets would add infra complexity
  for marginal user-visible benefit.
- **Telegram bot conversational interface.** ("/dca status") — fun but
  not on the critical path. v0.7+.
- **Multi-asset DCA UI.** Scaffold's in `strategies/multi_asset.py` from
  v0.5; ship the UI once the Business tier has a paying customer asking
  for it.
