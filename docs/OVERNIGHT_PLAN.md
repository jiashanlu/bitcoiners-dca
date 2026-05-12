# Overnight build plan — v0.5

Started 2026-05-11 ~midnight (Ben going to sleep). Goal: ship v0.5 fully
shipped + documentation refreshed, ready for Ben to wake up to.

## Hard constraints

- `dry_run: true` everywhere — never enable live trading
- Tests stay green throughout (currently 103/103)
- No external comms beyond team_feed + status Telegram to Ben
- No pushes to bitcoiners-ae main branch (dev only)
- Don't disturb the running Docker stack — rebuild at the end

## Phases

### Phase 1 — License / tier framework
- `core/license.py` — LicenseTier enum, LicenseManager, Ed25519 signature
- `utils/config.py` — LicenseConfig section
- Feature gates throughout (router, strategy, scheduler)
- `scripts/generate_license.py` — Ben's tooling to issue keys
- `bitcoiners-dca license` CLI — show current tier + features
- Tests for each tier's enforcement

### Phase 2 — Pro-tier strategy overlays
- New `core/strategies/` module
- `base.py` — StrategyOverlay protocol
- `dip.py` — refactor existing buy-the-dip out of Strategy
- `volatility.py` — realized-vol-weighted sizing
- `time_of_day.py` — DCA at the cheapest hour
- `drawdown.py` — extra buys at significant drawdowns from ATH
- Composable: each overlay returns (new_amount, applied_note)
- Config: `overlays.<name>.{enabled, params}`
- Tests per overlay

### Phase 3 — Hosted-tenant deployment template
- `hosted/` directory
- `docker-compose.tenant.yml` — per-customer compose
- `nginx.conf.template` — reverse-proxy snippets for app.bitcoiners.ae
- `provision.sh` — script for Ben to create a new tenant
- `tenants.example.yaml` — registry format

### Phase 4 — Documentation
- `docs/TIERS.md` — feature matrix per tier (Free / Pro / Business)
- `docs/STRATEGIES.md` — every overlay explained with math
- `docs/HOSTED_DEPLOYMENT.md` — Ben's hosting playbook
- README v0.5 status refresh
- Landing page on bitcoiners.ae — refresh tier descriptions + build status

### Phase 5 — First-run polish (if time)
- `bitcoiners-dca doctor` — system-check command
- Interactive `init-config --interactive` wizard
- Improved error messages on common misconfigurations

### Phase 6 — v0.6 scaffolding (stretch)
- Multi-asset DCA scaffold (BTC + ETH + SOL with allocation weights)
- Saudi market research notes (Rain API check via Playwright)
- KRW / TRY market candidate notes

### Phase 7 — Handoff
- Final summary doc `docs/V05_RELEASE_NOTES.md`
- Telegram summary message to Ben for morning
- Team-feed entries throughout
- Rebuild Docker stack with v0.5 code
- Verify `bitcoiners-dca validate` still passes
- Verify Docker stack starts cleanly

## Acceptance criteria

- 130+ tests passing (was 103)
- Every new feature has config knob + docstring + entry in docs/
- README + landing page reflect v0.5
- Docker stack live with v0.5 code, dry_run on
- Free tier installable + usable (single-exchange DCA + tax CSV)
- Pro tier features gated correctly when no license key
- Business tier scaffolded (basis-trade + LN Markets stubs)
