# bitcoiners-dca

Self-hostable, multi-exchange Bitcoin DCA bot for UAE residents. Smart-routes
buys to the cheapest exchange, surfaces arbitrage opportunities, auto-withdraws
to your hardware wallet, and produces UAE-tax-ready reports.

**Status:** v0.5 — license / tier framework · composable strategy overlays (volatility-weighted, time-of-day, drawdown-aware) · hosted-tenant deployment template · 130+/130+ tests passing. Builds on v0.4 (multi-hop smart routing · maker-mode execution · funding-rate monitor). All three UAE exchanges verified live (OKX BTC/AED, BitOasis BTC/AED + USDT/AED + BTC/USDT, Binance BTC/USDT via binance.com / ADGM).

**Free** = self-host, single exchange, base DCA + tax CSV + on-chain auto-withdraw. **Pro** (AED 49/mo) unlocks multi-exchange routing, maker mode, advanced strategies, Lightning auto-withdraw, funding-rate monitor. **Business** (AED 499+/mo) adds basis-trade execution, LN Markets covered calls, multi-asset DCA, family-office multi-strategy mode. See `docs/TIERS.md` for the full matrix.

---

## What this is

A long-running Python process that runs on YOUR infrastructure (Raspberry Pi,
Mac mini, Umbrel, Hetzner VPS, etc.) — never on a shared SaaS. It:

1. **Executes scheduled BTC buys** in AED across BitOasis, OKX, and Binance UAE
2. **Smart-routes** each buy to whichever exchange is cheapest *right now*
   (factoring in real-time ask + taker fee)
3. **Detects arbitrage** between exchanges and alerts you via Telegram —
   does not auto-execute (regulatory + operational risk)
4. **Auto-withdraws** accumulated BTC to your hardware-wallet address when a
   threshold is reached
5. **Logs every trade** to a local SQLite DB you own — easy backups, easy export

You hold your own exchange API keys. We never touch them.

---

## Architecture at a glance

```
                      ┌──────────────────────┐
                      │  config.yaml + .env  │
                      └──────────┬───────────┘
                                 │
                      ┌──────────▼───────────┐
                      │   CLI / Scheduler    │
                      └──────────┬───────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
        ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
        │ Strategy  │      │  Smart    │      │ Arbitrage │
        │  engine   │◄────►│  Router   │      │  Monitor  │
        └─────┬─────┘      └─────┬─────┘      └─────┬─────┘
              │                  │                  │
              └────────┬─────────┴─────────┬────────┘
                       │                   │
            ┌──────────▼──────┐    ┌───────▼──────────┐
            │  Exchange ABC   │    │   Notifier       │
            └──┬──────┬───┬───┘    │  (Telegram, etc) │
               │      │   │        └──────────────────┘
        ┌──────▼──┐ ┌─▼─┐ ┌▼─────────┐
        │ OKX     │ │BNB│ │BitOasis  │
        │ adapter │ │   │ │ adapter  │
        └─────────┘ └───┘ └──────────┘
               │
        ┌──────▼────────────────────────┐
        │  SQLite (trades, arb, cycles) │
        └───────────────────────────────┘
```

---

## Quick start

```bash
# 1. Install (when published)
pip install bitcoiners-dca

# OR run from source:
git clone https://github.com/jiashanlu/bitcoiners-dca.git
cd bitcoiners-dca
pip install -e .

# 2. Write a starter config
bitcoiners-dca init-config

# 3. Edit config.yaml + set environment variables for your exchange API keys
export OKX_API_KEY=...
export OKX_API_SECRET=...
export OKX_API_PASSPHRASE=...
export TG_BOT_TOKEN=...

# 4. Smoke test: show prices
bitcoiners-dca prices

# 5. Run one DCA cycle (dry-run by default — set dry_run: false in config to go live)
bitcoiners-dca buy-once

# 6. Check for arbitrage opportunities
bitcoiners-dca arb-check

# 7. View status + lifetime stats
bitcoiners-dca status
```

### Docker

```bash
docker build -t bitcoiners-dca .

docker run -d --name dca \
  -v $PWD/config.yaml:/app/config.yaml \
  -v $PWD/data:/app/data \
  -v $PWD/reports:/app/reports \
  -e OKX_API_KEY=... \
  -e OKX_API_SECRET=... \
  -e OKX_API_PASSPHRASE=... \
  -e TG_BOT_TOKEN=... \
  bitcoiners-dca buy-once
```

---

## Config reference

See `config.example.yaml`. Key sections:

- **strategy**: amount, frequency, day, time, timezone
- **overlays.buy_the_dip**: if BTC is down N% in 7 days, multiply buy amount by M
- **routing**: best_price (default) or pin a preferred exchange
- **exchanges**: enable per-exchange, point at env vars for credentials
- **auto_withdraw**: hardcode YOUR hardware wallet address; bot withdraws above threshold
- **arbitrage**: alert threshold (default 1.5% net of fees)
- **notifications.telegram**: bot token + your chat ID
- **dry_run**: master simulate-only switch

---

## What "smart routing" actually means

For each scheduled buy, the bot:

1. Queries ticker (bid/ask) from all enabled exchanges *in parallel*
2. Queries fee schedule from each
3. Computes `effective_price = ask × (1 + taker_fee)` per exchange
4. Filters out exchanges with spread > config threshold (signals thin book)
5. Applies user preference bonus if configured
6. Picks the lowest effective_price

Routing reasoning is logged for every cycle so you can audit decisions later.

---

## Arbitrage: detection, not execution

The bot **does not auto-execute arbitrage trades.** Reasons:

- Cross-exchange BTC withdrawals take minutes-to-hours; the spread usually closes first
- Auto-executing arbitrage in the UAE is regulatorily ambiguous (potentially VASP activity)
- Slippage on real-world fills can easily eat the entire detected spread

Instead, the bot detects opportunities and sends Telegram alerts. You decide
whether to act manually.

Detection accounts for:
- Buy-side taker fee
- Sell-side taker fee
- BTC withdrawal fee (approximate %)
- Slippage buffer (configurable, default 0.3%)

Only opportunities with **positive net profit after all costs** are alerted on.

---

## Security model

| Surface | What we do |
|---|---|
| **Your exchange API keys** | Read from env vars; never logged, never sent over network except to exchanges |
| **Withdrawal address** | Hardcoded in config.yaml; bot cannot change it at runtime (no UI input) |
| **Withdrawal scope** | Most exchanges support trade-only API keys. We strongly recommend that scope for the bot's key. Set up a separate withdraw-enabled key only for auto-withdraw if you want it. |
| **State storage** | SQLite on your machine. No telemetry, no cloud sync. |
| **Tax reports** | Generated locally, never shipped anywhere. |

---

## Roadmap

- [x] Project scaffolding + Exchange ABC
- [x] OKX adapter (end-to-end via ccxt)
- [x] Binance adapter (ccxt-based, **binance.com via ADGM** — no BTC/AED pair, BTC/USDT only)
- [x] BitOasis adapter scaffold (custom REST)
- [x] Smart router with weighted scoring
- [x] Arbitrage monitor with fee-aware net-profit calc
- [x] DCA strategy engine with buy-the-dip overlay
- [x] Auto-withdraw to hardware wallet
- [x] SQLite persistence layer
- [x] CLI (buy-once, arb-check, prices, status, init-config)
- [x] Dockerfile
- [x] Telegram notifications
- [x] Internal scheduler loop (`bitcoiners-dca run` daemon, apscheduler-based)
- [x] FastAPI read-only web dashboard
- [x] UAE-format tax CSV export
- [x] Property-based tests for routing + arbitrage math (9 passing)
- [x] docker-compose + systemd deployment doc
- [x] BitOasis adapter — verified against api.bitoasis.net/doc/ (Bearer auth, /v1 endpoints, BTC-AED pair format, live ticker confirmed)
- [ ] Multisig output address rotation
- [x] Lightning withdrawal support (OKX — `bitcoiners-dca withdraw lnbc1… 0.005`)
- [x] Multi-hop routing (AED→USDT→BTC on the same exchange; ~0.09% advantage on OKX)
- [x] Cross-exchange route alerts (Telegram-only; manual execution above 25k AED)
- [x] Maker-mode execution (taker / maker_only / maker_fallback)
- [x] Funding-rate monitor (basis-trade signals, detection only)
- [x] `routes` + `funding` + `license` audit CLIs
- [x] License framework — Ed25519-signed offline-verifiable tokens; tier gating throughout
- [x] Volatility-weighted DCA overlay (buy less when realized vol is high)
- [x] Time-of-day overlay (skip cycles outside cheapest hours; or scale by hourly spread)
- [x] Drawdown-aware sizing (extra buys at -20% / -40% / -60% from ATH)
- [x] Hosted-tenant deployment template (`hosted/provision.sh` + per-tenant compose + nginx fragment)
- [ ] On-chain consolidation across multiple exchange outputs
- [x] Umbrel community-app package (manifest + compose ready at `umbrel/` — pending Docker image push)
- [x] Risk-manager circuit breakers (daily cap, single-buy cap, auto-pause on consecutive failures)
- [x] Backtest engine (`bitcoiners-dca backtest --days 365 --dip --dip-threshold -7`)

---

## Project layout

```
bitcoiners-dca/
├── pyproject.toml
├── Dockerfile
├── config.example.yaml
├── src/
│   └── bitcoiners_dca/
│       ├── cli.py            # entry point
│       ├── core/
│       │   ├── models.py     # exchange-agnostic data types
│       │   ├── strategy.py   # DCA engine
│       │   ├── router.py     # smart routing
│       │   ├── arbitrage.py  # opportunity detection
│       │   └── notifications.py
│       ├── exchanges/
│       │   ├── base.py       # Exchange ABC
│       │   ├── okx.py        # ccxt-based, working
│       │   ├── binance.py    # ccxt-based, working
│       │   └── bitoasis.py   # custom REST, scaffold
│       ├── persistence/
│       │   └── db.py         # SQLite + queries
│       ├── utils/
│       │   └── config.py     # YAML loader + validation
│       └── web/              # FastAPI dashboard (planned)
├── tests/
└── data/                     # SQLite DB lives here
```

---

## License

MIT. Software you run on your hardware with your keys. If we sold this as a
hosted service, that would be a VASP-licensed activity in the UAE. Selling it
as software is not.

---

## Built by [bitcoiners.ae](https://bitcoiners.ae)
