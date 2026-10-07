# bitcoiners-dca — Claude context

> Inherits from workspace root `CLAUDE.md`. This file is repo-specific.

## What this is

Self-hostable multi-exchange BTC DCA bot for UAE residents. Python 3.11+ long-
running process. v0.6: self-service dashboard, hot-reload daemon, encrypted-
at-rest credentials (Fernet).

Free and open source (MIT, see `LICENSE`) with NO tiers — decided
2026-10-07 ("unlock everything, get rid of Pro"). Every feature
(multi-exchange smart routing incl. multi-hop, maker mode, all overlays,
on-chain smart triggers, funding monitor) is always available; the user's
config is used as-is. There is no licence check and no remote Pro API.
Legacy `license:` sections in config.yaml are accepted and ignored
(`utils/config.py::_RETIRED_SECTIONS`).

Note: auto-withdraw is parked until Lightning withdraw lands — on-chain fees wipe out small-cycle savings. Manual withdraw via /withdrawals/withdraw-now is the supported flow. See [[feedback-kill-auto-withdraw-until-lightning]].

## Stack

- Python 3.11+, packaged with setuptools
- ccxt (OKX, Binance UAE) + custom BitOasis httpx adapter
- pydantic v2 configs, PyYAML
- apscheduler (cron-style scheduler)
- FastAPI + jinja2 (customer dashboard)
- python-telegram-bot, tenacity, rich, typer
- cryptography (Fernet for secret-at-rest)
- pytest, mypy, ruff (dev)

## Dev

```bash
pip install -e ".[dev]"
bitcoiners-dca init-config         # writes config.yaml
# export exchange env vars + TG_BOT_TOKEN
bitcoiners-dca prices              # smoke test
bitcoiners-dca buy-once            # dry-run unless dry_run: false
pytest                             # 390+ tests should pass
ruff check src tests
mypy src
```

## Architecture

Strategy engine → smart router → exchange ABC (OKX/BNB/BitOasis adapters)
→ SQLite (trades, arb, cycles) → notifier (Telegram).

Hosted DCA (per-customer tenants on Hetzner + `hosted/` provisioner) was
retired 2026-10-07 and the `hosted/` tree deleted; the bot is self-host
only. Ben's own instance runs on
home CT113 `dca-bot` (192.168.4.213, proxmox2, HA + */5 replication to
proxmox1) at `/opt/bitcoiners-dca/tenants/benbois-ae0e0001`. Its
dashboard is bound to 127.0.0.1:8100 and published only through the
CT113 cloudflared tunnel at `dca.bitcoiners.ae` behind Cloudflare Access.
Hosted-era docs live in `docs/archive/` for history.

## Deploy

Push to Gitea `jiashan-dev/bitcoiners-dca` →
- CI builds the image on dockers-LXC and runs the test gate against it.
- `dev` → build + test only, nothing is shipped.
- `main` / `v*` tag → image is loaded onto CT113 (192.168.4.213).
- **CI does NOT recreate the running bot.** Recreate it manually after
  the image lands:

```bash
ssh root@192.168.4.213
cd /opt/bitcoiners-dca/tenants/benbois-ae0e0001
docker compose up -d --force-recreate
# verify code is live:
docker exec bitcoiners-dca-benbois-ae0e0001-dashboard grep -l '<marker>' /app/src/...
```

## Hard rules in this repo

- **DRY_RUN=true** until Ben explicitly toggles it. Same applies to
  `[[feedback_no_destructive_diagnostics]]` — read-only probes only when
  debugging tenant state.
- **No tiers, no feature gating** — don't reintroduce licence keys,
  tier checks or a remote "Pro API" path. Every feature ships to everyone.
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
- **Test pyramid is the contract** — 390+ tests; pre-merge run is required.
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
- Per-feature docs: `docs/ROUTING.md`, `docs/STRATEGIES.md`, `docs/EXECUTION_MODES.md`, `docs/FUNDING_MONITOR.md`
- Retired hosted/licensing docs (history only): `docs/archive/`

## Where to look for more

- Hosted arch: [[bitcoiners_dca_hosted_arch]]
- v0 build state: [[dca_bot_v0_build]]
- Product roadmap: [[dca_bot_product_roadmap]]
- Regulatory research: [[dca_bot_regulatory_research]]
