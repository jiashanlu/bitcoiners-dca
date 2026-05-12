# Funding-rate monitor

A passive watcher for BTC perpetual funding rates. When the annualized
rate crosses your threshold, you get a Telegram alert. **Detection only.**
The bot never places a derivatives trade.

```yaml
funding_monitor:
  enabled: false                    # opt-in
  poll_interval_seconds: 3600       # 1 hour
  alert_threshold_pct: 15.0         # alert when ann ≥ this %
  alert_negative_threshold_pct: -10.0
  alert_cooldown_hours: 24
  instruments:
    - {exchange: okx, symbol: BTC-USDT-SWAP}
```

---

## What's a funding rate?

Perpetual futures (perps) don't expire. To keep their price anchored to
spot, exchanges charge a periodic payment between long and short
positions:

- **Positive funding** → longs pay shorts. Means more demand for long
  exposure than supply — perp price drifting above spot.
- **Negative funding** → shorts pay longs. Bearish perp positioning.

OKX settles funding every 8 hours, so a single 8h rate of +0.01% is
equivalent to `0.01% × 3 × 365 = +10.95% annualized` if it persisted.

---

## Why we monitor

The classic **cash-and-carry basis trade** profits from sustained
positive funding:

1. Buy 1 BTC spot (long delta +1)
2. Open 1 BTC short on the perp (delta -1)
3. Net delta = 0 → BTC price doesn't matter
4. You collect the funding rate every 8 hours

If funding sustains at +20% annualized for a quarter, you earn 5% in
USDT regardless of where BTC goes. This was real money during the 2021
and 2024 bull manias — funding peaked at +50% APY in May 2024.

Right now (May 2026), funding is *negative on average* (-0.81% annualized
over the last 30 days). The basis trade is dead in this regime — you'd
be paying, not earning. That's exactly why we monitor: most of the year
this is uninteresting, but a flip to >+15% APY changes the picture.

---

## Reading the alerts

Alert format on Telegram:

> Funding spike on OKX BTC-USDT-SWAP: +18.50% annualized (longs paying
> shorts). Next settle at 2026-05-12T08:00:00+00:00.

When you see this, the bot is saying "the math just got attractive."
It is *not* saying "execute the basis trade now."

**Before acting, check:**

1. **Is funding sustained or a spike?** A single +18% reading on a
   thin order book might revert next period. Look at the 30-day average
   (use `bitcoiners-dca funding --history`).
2. **Do you have the collateral?** A 1 BTC basis trade needs ~30k USDT
   minimum on OKX as initial margin, plus a buffer.
3. **Are you OK with the operational risk?** Exchange downtime during
   the trade window can result in forced liquidations.

For UAE residents specifically: derivatives are VARA-regulated. Using
OKX UAE's derivatives stays inside the licensed perimeter; bridging to
non-UAE venues (e.g. Binance perps) may not.

---

## CLI

```bash
# Current funding rate across configured instruments
bitcoiners-dca funding

# With 30-day average + range
bitcoiners-dca funding --history
```

Example output (live):

```
BTC perpetual funding (live)
┌──────────┬───────────────┬───────────┬────────────┬───────────────────┐
│ Exchange │ Instrument    │ 8h rate   │ Annualized │ Next settle (UTC) │
├──────────┼───────────────┼───────────┼────────────┼───────────────────┤
│ OKX      │ BTC-USDT-SWAP │ -0.00371% │ -4.06%     │ 2026-05-12 08:00  │
└──────────┴───────────────┴───────────┴────────────┴───────────────────┘
  30-day avg: -0.81% ann | 37/90 fundings positive | range -0.01095% to +0.01000%
```

---

## Cooldown

Once an alert fires for an (exchange, instrument), no further alert
fires for the same pair for `alert_cooldown_hours` (default 24h). This
avoids spamming you while funding stays elevated. The cooldown timer is
stored in the SQLite `meta` table — restarts don't reset it.

Cooldown is per-direction-but-not-really: if funding crosses
**either** threshold (positive or negative) the same cooldown applies.
This is intentional — flipping rapidly between regimes is itself
information, but doesn't warrant 3 alerts per day.

---

## What's not in v0.4

- **Auto-executing basis trades.** Operational risk too high; build only
  after a regime flip makes it worth the effort.
- **Funding sources beyond OKX.** Binance perps are accessible via
  binance.com but UAE customers may face restrictions. Bybit is
  UAE-blocked. We'll add more sources when there's a reason.
- **Calendar-spread basis** (dated futures vs spot). The math is there
  but right now the carry is ~1.8% annualized — worse than UAE T-bills.
