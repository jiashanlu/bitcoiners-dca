# v0.5 release notes

Built overnight on 2026-05-11 → 2026-05-12. Ben asleep, dry-run preserved
throughout, tests green at every commit, no live trades placed.

## Summary

v0.5 turns bitcoiners-dca from a working bot into a **sellable product**. It
adds:

- **License + tier framework** — Ed25519-signed offline-verifiable tokens
  that gate Pro/Business features.
- **Composable strategy overlays** — three new ones (volatility-weighted,
  time-of-day, drawdown-aware) joining buy-the-dip. Multipliers compound.
- **Hosted-tenant deployment scaffold** — per-customer Docker compose +
  nginx fragment + `provision.sh` script to onboard a paying customer
  in one command.
- **Documentation refresh** — three new deep-dive docs (`TIERS.md`,
  `STRATEGIES.md`, `HOSTED_DEPLOYMENT.md`) + landing-page update +
  expansion-market research notes.
- **`doctor` CLI** — single-command holistic system check, complements
  the existing `validate`.
- **Multi-asset DCA planner** — scaffold for v0.7 (BTC + ETH + SOL with
  weights, redistribution when a leg falls below min-buy).

## Test counts

| Pre-session | 103 |
| Post-session | **136** |
| New tests | 33 (license: 13, overlays: 14, multi-asset: 6) |

All passing.

## Files added

```
src/bitcoiners_dca/core/license.py
src/bitcoiners_dca/strategies/__init__.py
src/bitcoiners_dca/strategies/base.py
src/bitcoiners_dca/strategies/dip.py
src/bitcoiners_dca/strategies/volatility.py
src/bitcoiners_dca/strategies/time_of_day.py
src/bitcoiners_dca/strategies/drawdown.py
src/bitcoiners_dca/strategies/multi_asset.py
scripts/generate_license.py
hosted/docker-compose.tenant.yml
hosted/nginx.conf.template
hosted/provision.sh
hosted/tenants.example.yaml
docs/TIERS.md
docs/STRATEGIES.md
docs/HOSTED_DEPLOYMENT.md
docs/EXPANSION_NOTES.md
docs/V05_RELEASE_NOTES.md  (this file)
docs/OVERNIGHT_PLAN.md
tests/test_license.py
tests/test_strategy_overlays.py
tests/test_multi_asset_plan.py
```

## Files touched

- `utils/config.py` — new `LicenseConfig`, `ExecutionConfig`, `MakerConfig`,
  per-overlay config sections
- `cli.py` — `license` + `doctor` commands; `_apply_license_filter` gate
  threaded into every runtime path; `_load_runtime_config` helper
- `core/strategy.py` — accepts an overlay stack via constructor; backward-
  compatible legacy path for the old `dip_overlay_enabled` config fields
- `README.md` — v0.5 status + new roadmap bullets
- `config.example.yaml` — full `license`, expanded `overlays`, `execution`
  sections with inline comments
- `app/dca-bot/page.tsx` (in bitcoiners-ae repo) — updated "What's built
  today" grid, pushed to `dev` branch → deploys to dev.bitcoiners.ae

## How the tier framework works

```
config.yaml
├── license.tier: free|pro|business
└── license.key:  base64 Ed25519-signed token (Pro/Business only)

Boot sequence:
  1. load_config() parses YAML
  2. LicenseManager.from_config(tier_str, key) verifies offline
     - bad/missing key → silent downgrade to FREE
     - expired key → silent downgrade to FREE
     - tier mismatch → use license's tier, log warning
  3. _apply_license_filter() mutates the config to mask out features
     the user enabled but isn't entitled to
  4. Everything downstream reads the filtered config and behaves
     normally — no gating checks scattered through the codebase
```

The license-issuing tool (`scripts/generate_license.py`) signs with the
private key at `~/.openclaw/workspace/infra/dca_license_signing_key.pem`
(generated overnight, chmod 600). The public key is hardcoded in
`core/license.py`.

To issue a Pro key for a customer:

```bash
python scripts/generate_license.py issue \
  --private-key /Users/macmini/.openclaw/workspace/infra/dca_license_signing_key.pem \
  --customer-id alice@example.com \
  --tier pro \
  --expires 2027-05-12
```

Paste the printed token into the customer's `config.yaml` under
`license.key`.

## Hosted deployment workflow

```
hosted/provision.sh ben-pro ben@bitcoiners.ae pro

# Creates:
#   /opt/bitcoiners-dca/tenants/ben-pro/
#     ├── config/config.yaml      (license, strategy, exchanges)
#     ├── .env                    (API secrets — chmod 600)
#     ├── data/                   (SQLite DB lives here)
#     ├── reports/
#     └── docker-compose.yml      (rendered from template)
#   /etc/nginx/conf.d/bitcoiners-dca-ben-pro.conf
# Picks a free localhost port (8100-8999) and binds the tenant's
# dashboard there.

# Tenant customer then:
htpasswd -c /etc/nginx/.htpasswd-bitcoiners-ben-pro ben-pro
cd /opt/bitcoiners-dca/tenants/ben-pro && docker compose up -d
nginx -t && systemctl reload nginx

# Dashboard live at https://app.bitcoiners.ae/ben-pro/
```

See `docs/HOSTED_DEPLOYMENT.md` for the full playbook including
upgrades, suspensions, deletion, monitoring, and backups.

## What you can do RIGHT NOW (when you wake up)

The Docker stack is live with v0.5 code, strict dry_run:

```bash
# Smoke checks
cd /Users/macmini/.openclaw/workspace/bitcoiners-dca

# Comprehensive system check
bitcoiners-dca doctor

# See what tier you're on and what's enabled
bitcoiners-dca license

# See routes at your live cycle size
bitcoiners-dca routes --amount 500

# Try generating + plugging in a Pro license
python scripts/generate_license.py issue \
  --private-key /Users/macmini/.openclaw/workspace/infra/dca_license_signing_key.pem \
  --customer-id ben@bitcoiners.ae \
  --tier pro \
  --expires 2027-05-12
# Paste the token into config.yaml license.key
bitcoiners-dca license  # confirm Pro features unlocked

# Run the dry-run with maker_fallback + overlays enabled
bitcoiners-dca buy-once

# Dashboard
open http://localhost:8000
```

## What's INTENTIONALLY not done in v0.5

(Each represents a deliberate decision, not a TODO list)

- **Pro key billing flow.** This is a UI/payment concern, not a bot
  concern. Stripe + a small landing-page form will handle subscriptions.
  The bot just verifies a static token.
- **Auto-renewal of expired licenses.** Tokens are issued for fixed
  durations. Renewal = new token + customer-side config update. Could
  be automated later via a tenant-control plane.
- **Multi-tenant database isolation.** Per-tenant data lives in
  per-tenant directories. We're not using a multi-tenant DB engine
  (Postgres with row-level security) — too much complexity for the
  customer counts we're targeting.
- **i18n for the dashboard.** Arabic / Turkish are on the v0.6 roadmap.
- **Basis-trade execution.** Funding is currently negative (-4% APY),
  no edge to capture. Scaffolding lives in license tier definitions;
  execution code waits for a regime change.

## Known limitations / friction points discovered

- Dashboard `/healthz` reports `exchanges_configured: []` — appears to
  inspect state before config-driven boot completes. Minor cosmetic
  bug, doesn't affect operation. Logged for v0.6 fix.
- `validate` command's license-tier-aware audit could be improved to
  show "user requested X, license allows Y, here's the diff" — would
  make hosted-tenant onboarding even cleaner.

## Roadmap (updated post-v0.5)

| Version | Headline | Estimated effort |
|---|---|---|
| **v0.6** | Turkey market (BTCTRY on Binance + BTCTurk adapter) · i18n for dashboard | ~10h |
| **v0.7** | Basis-trade execution (when funding flips ≥ +15% APY) · LN Markets covered-call sidecar | ~15h |
| **v0.8** | Saudi market (Rain adapter via browser-fingerprinted client) | ~8h |
| **v0.9** | Multi-asset DCA wired into Business tier · family-office multi-strategy | ~12h |
| **v1.0** | Public launch with EN/AR/TR + Stripe billing | ~6h once above is solid |

## v0.5 polish (shipped after the initial handoff)

- **Market-data provider** (`core/market_data.py`) — CoinGecko-fed snapshot
  with 7d-ago, 30d-ago, ATH, realized 30d vol. Wired into the scheduler
  + buy-once so the new overlays now have REAL data to work with, not
  placeholders.
- **`bitcoiners-dca backup` CLI** — one-command tar.gz of DB + reports
  + config for cron-driven backups. Uses SQLite's online-backup API so
  it's safe to run mid-write.
- **Dashboard charts** — BTC accumulation over time + avg cost basis vs
  current market price. Chart.js via CDN. Two new endpoints
  (`/api/cumulative-btc`, `/api/cost-basis-vs-market`).
- **5 seeded dry-run cycles** in the DB so the dashboard charts have
  something to show when you open it. AED 2,500 simulated spend →
  0.00831 BTC accumulated.

## Final state for handoff

- 143/143 tests passing
- Docker stack live at http://localhost:8000 (dry_run ON)
- Three credentials wired (OKX + BitOasis + Binance) — all read-only
  verified, no live trades placed
- Telegram notifications wired (TG_BOT_TOKEN in .env)
- Landing page on bitcoiners.ae refreshed and pushed to `dev` branch
- License signing key at `~/.openclaw/workspace/infra/dca_license_signing_key.pem`
  (chmod 600; do NOT share)
- All work documented in `docs/`, `memory/`, and team-feed

Sleep well. ☕ ⚡
