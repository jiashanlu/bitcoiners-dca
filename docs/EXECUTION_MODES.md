# Execution modes — taker vs maker

How the bot places each buy. Configured per cycle (one mode applies to all
hops in a route).

```yaml
execution:
  mode: taker                       # taker | maker_only | maker_fallback
  maker:
    limit_at: bid                   # bid | midpoint | ask_minus_bps
    spread_bps_below_market: 5
    timeout_seconds: 600
```

---

## taker (default)

Market order. Crosses the spread immediately, pays the full taker fee.

| | |
|---|---|
| **Fill probability** | 100% |
| **Time to fill** | Instant |
| **Fee** | Taker (0.5% on BitOasis, 0.15% on OKX, 0.1% on Binance) |
| **When to use** | Default. Predictable. Safe for tight DCA schedules. |

---

## maker_only

Place a limit order priced to be a maker. Wait `timeout_seconds`. If it
doesn't fill, cancel and **skip the cycle entirely**.

| | |
|---|---|
| **Fill probability** | High in flat markets, low when BTC is gapping up |
| **Time to fill** | Variable (seconds to timeout) |
| **Fee** | Maker (typically ~50% of taker — 0.2% on BitOasis, 0.08% on OKX, 0.075% on Binance) |
| **When to use** | You're fee-obsessed and willing to occasionally skip a buy. |

The risk: in a sustained uptrend the limit never gets hit, so you miss
cycles and skew your average-cost upwards over time. Don't use
`maker_only` if you're trying to stack on a strict schedule.

---

## maker_fallback (recommended for most users)

Place a limit. If it doesn't fill at the timeout, **cancel and market-buy**
instead. Best of both worlds: capture the maker fee when conditions
permit, but never miss a cycle.

| | |
|---|---|
| **Fill probability** | 100% (falls back to market) |
| **Time to fill** | Up to `timeout_seconds`, then instant |
| **Fee** | Maker on most cycles, taker on the misses |
| **When to use** | You want fee savings without giving up cycle reliability. |

**Recommended starter settings:**

```yaml
execution:
  mode: maker_fallback
  maker:
    limit_at: bid                   # most-likely-to-fill price
    timeout_seconds: 600            # 10 min — enough for most pullbacks
```

---

## Maker pricing strategies

`limit_at` controls where you place the limit relative to the order book:

| Setting | Where it sits | Fill probability | Fee captured |
|---|---|---|---|
| `bid` | At/just below best bid | High | Maker |
| `midpoint` | Halfway between bid and ask | Medium | Maker |
| `ask_minus_bps` | Inside the spread (ask × (1 - 5bps)) | Low | Maker, lowest cost |

`spread_bps_below_market` only applies when `limit_at: ask_minus_bps`.

---

## Per-exchange notes

### OKX

Maker fee = 0.10%, taker fee = 0.15%. Spread saved per fill: 0.05%. On a
~1000 AED cycle, that's AED 0.50. Over 52 cycles/year (weekly DCA at AED
20k/month), that's ~AED 26/year — meaningful but not life-changing on
its own.

### BitOasis

Maker fee = 0.20%, taker fee = 0.50%. Spread saved per fill: **0.30%** —
substantial. On weekly AED 500 cycles, that's AED 1.50/cycle = ~AED 78/year.
This is where maker mode adds the most value.

### Binance UAE (via binance.com / ADGM)

Maker fee = 0.075%, taker fee = 0.100%. Tiny edge (0.025%) — and Binance
BTC/USDT spread is already <0.001%, so your limit will fill almost
instantly anyway. Maker mode here is basically free alpha.

---

## Why we treat "didn't fill" as skip-not-failure

In `maker_only` mode, an order timing out is *expected behavior*, not a
malfunction. The bot's risk manager treats it as a skip — it does NOT
count toward the consecutive-failure circuit breaker. You'll see a note
in the cycle log: `Hop 1/1: maker_only limit timed out, cycle skipped`.

Telegram notifications still fire so you know a cycle was skipped, but
no error escalation happens. If you're seeing this constantly (e.g. >50%
of cycles skipped), tune `limit_at` looser or switch to `maker_fallback`.
