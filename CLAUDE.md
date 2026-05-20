# bitcoiners-dca — Claude context

> Inherits from workspace root `CLAUDE.md`. This file is repo-specific.

## What this is

Self-hostable multi-exchange BTC DCA bot for UAE residents. Python 3.11+ long-
running process. v0.6: customer-facing dashboard, hot-reload daemon, encrypted-
at-rest credentials (Fernet), license + tier framework.

Free = single exchange + DCA + tax CSV + manual on-chain withdraw.
Pro = multi-exchange smart routing (3-hop), maker mode, advanced strategies, on-chain smart triggers (MVRV-Z), funding monitor.
Business = basis trade, LN Markets covered calls, multi-asset DCA.

Note: auto-withdraw is parked until Lightning withdraw lands as a Pro feature — on-chain fees wipe out AED 49 customer savings. Manual withdraw via /withdrawals/withdraw-now is the supported flow. See [[feedback-kill-auto-withdraw-until-lightning]].

## Stack

- Python 3.11+, packaged with setuptools
- ccxt (OKX, Binance UAE) + custom BitOasis httpx adapter
- pydantic v2 configs, PyYAML
- apscheduler (cron-style scheduler)
- FastAPI + jinja2 (customer dashboard)
- python-telegram-bot, tenacity, rich, typer
- cryptography (license signing + Fernet for secret-at-rest)
- pytest, mypy, ruff (dev)

## Dev

```bash
pip install -e ".[dev]"
bitcoiners-dca init-config         # writes config.yaml
# export exchange env vars + TG_BOT_TOKEN
bitcoiners-dca prices              # smoke test
bitcoiners-dca buy-once            # dry-run unless dry_run: false
pytest                             # 165+ tests should pass
ruff check src tests
mypy src
```

## Architecture

Strategy engine → smart router → exchange ABC (OKX/BNB/BitOasis adapters)
→ SQLite (trades, arb, cycles) → notifier (Telegram).

Hosted tenants run the same code; tenant config + secrets live in the
tenant container's mounted volume on tenants-LXC `192.168.4.160`. Tenants
are provisioned by `hosted/provision.sh`.

## Deploy

Push to Gitea `jiashan-dev/bitcoiners-dca` →
- CI builds image tarball on dockers-LXC →
- `dev` branch → tarball lands on tenants-LXC →
- `main` / tag → tarball also goes to Hetzner prod →
- **CI does NOT recreate running tenants.** Manual recreate per tenant
  required after image lands.

Recreate cadence:
```bash
ssh tenants-lxc
cd /opt/tenants/<tenant>
docker compose up -d --force-recreate
# verify code is live:
docker exec <container> grep -l '<marker>' /app/src/...
```

## Hard rules in this repo

- **DRY_RUN=true** until Ben explicitly toggles it. Same applies to
  `[[feedback_no_destructive_diagnostics]]` — read-only probes only when
  debugging tenant state.
- **License framework is load-bearing** — Pro/Business features gate on
  `cryptography`-signed license keys. Never bypass the check "just for
  local testing"; use a valid signed dev key from `scripts/generate_license.py`.
- **Manual withdraw is the only withdraw surface** — auto-withdraw was
  retired (see [[feedback-kill-auto-withdraw-until-lightning]]). Don't
  add a "set auto-withdraw destination" API endpoint or surface auto-
  withdraw fields in the dashboard. The DB schema + adapters stay as
  plumbing; the daemon path is gone.
- **Arbitrage is detect-only** — alerts go to Telegram, no auto-execute.
  Cross-exchange withdrawals + UAE regulatory ambiguity make auto-exec
  unsafe.
- **Exchange API keys**: read from env vars only, never log, never echo
  back to clients in the dashboard. Recommend trade-only scope; document
  withdraw-scope key separately.
- **Test pyramid is the contract** — 165+ tests; pre-merge run is required.
  No skipping integration tests with mocks of exchange responses we
  haven't seen ([[exchange_whitelist_api_surface]] — only Binance exposes
  list-whitelist; OKX/BitOasis don't).
- **UAE Travel Rule on Binance**: withdrawals route through
  `/sapi/v1/localentity/withdraw/apply` with the UAE questionnaire JSON,
  not the generic endpoint ([[binance_uae_travel_rule]]).

## Useful paths

- Exchange adapters: `src/bitcoiners_dca/exchanges/`
- Smart router: `src/bitcoiners_dca/routing/`
- Strategy engine + overlays: `src/bitcoiners_dca/strategies/`
- Dashboard (FastAPI + jinja2): `src/bitcoiners_dca/web/`
- License framework: `src/bitcoiners_dca/license/`
- Hosted provisioning: `hosted/`
- Per-feature docs: `docs/TIERS.md`, `docs/HOSTED_DEPLOYMENT.md`, `docs/ROUTING.md`

## Where to look for more

- Hosted arch: [[bitcoiners_dca_hosted_arch]]
- v0 build state: [[dca_bot_v0_build]]
- Product roadmap: [[dca_bot_product_roadmap]]
- Regulatory research: [[dca_bot_regulatory_research]]
