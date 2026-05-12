# Routing

The bot's job, on every DCA cycle, is to take your quote currency (AED) and
turn it into the maximum amount of BTC, given:

- which exchanges you've enabled (BitOasis, OKX, Binance UAE)
- where your funds are
- live order-book prices and taker fees on every pair
- your preference for one exchange over another (optional)

Routing is *not* just "pick the cheapest exchange." It enumerates every
viable *path* from AED to BTC and picks the path with the lowest effective
cost per BTC after fees.

---

## Route types

### 1. Direct routes

One hop, one order. `BTC/AED` listed natively on the exchange. Example:

```
okx · BTC/AED · buy
  effective price = ask × (1 + taker_pct)
```

Available on OKX and BitOasis. **Not** available on Binance — `binance.com`
doesn't list any BTC/AED pair (verified May 2026).

### 2. Same-exchange two-hop routes

Two orders on the same exchange, chained: `AED → USDT → BTC`. The bot
sells AED for USDT on the exchange's `USDT/AED` pair, then immediately
buys BTC for the resulting USDT on the same exchange's `BTC/USDT` pair.

```
okx · USDT/AED · buy   (hop 1)
  → received_usdt = quote / (ask × (1 + taker))
okx · BTC/USDT · buy   (hop 2)
  → received_btc = received_usdt / (ask × (1 + taker))
```

**Why it wins:** OKX's `BTC/AED` pair carries a wider implicit spread than
the synthetic `BTC/USDT × USDT/AED`. Even paying two taker fees, the net
yield is better. At present market structure (May 2026), OKX two-hop
beats OKX direct by ~0.09% and beats BitOasis direct by ~0.34%.

Enable with `routing.enable_two_hop: true`. Audit with the
`bitcoiners-dca routes` command.

### 3. Cross-exchange routes (alerts only)

`AED → USDT on exchange A → withdraw USDT to exchange B → buy BTC on
exchange B`. These have a fixed cost (the USDT withdrawal fee — about 1.5
USDT on OKX TRC20) plus transit time (1-10 minutes for TRC20).

```
okx · USDT/AED · buy
  → received_usdt = quote / (ask × (1 + taker))
okx → binance · withdraw   (fixed cost: 1.5 USDT in TRC20)
  → received_after_xfer = received_usdt - 1.5
binance · BTC/USDT · buy
  → received_btc = received_after_xfer / (ask × (1 + taker))
```

**The bot does NOT auto-execute cross-exchange routes.** Two reasons:

1. **Transit time creates price risk.** During the 1-10 minute USDT
   withdrawal, BTC can move ±0.5%+, which dwarfs the typical 0.05-0.15%
   bridge advantage.
2. **Orphaned-state cleanup is brittle.** If the withdrawal sticks (Tron
   RPC issue, exchange maintenance, address mismatch), your funds end up
   stuck in transit. We don't want to write the state machine that
   reconciles that.

What the bot *does*: surface cross-exchange opportunities as alerts when
they're net-positive at your configured cycle size (default
`cross_exchange_min_size_aed: 25000`). You decide whether to execute
manually.

---

## Picking math

For each candidate route, the router computes:

```
effective_price = input_amount / route.expected_output(input_amount)
```

where `expected_output` simulates each hop's `quote / (price × (1 + taker))`.

Spread filter: routes whose hops have spread above `exclude_if_spread_pct_above`
(default 2%) are dropped, unless every candidate is wide (in which case
all are kept).

Balance filter: when the cycle has a known `required_quote_amount`, routes
whose first-hop exchange has insufficient quote-currency balance are
dropped. (If every route is underfunded, all are kept so the caller gets
a clear `InsufficientBalanceError` with a sensible default.)

Preference bonus: when `routing.preferred_exchange: X` is set, the score
for routes whose first hop is on `X` is multiplied by
`(1 - preferred_bonus_pct/100)`. With a 0.5% bonus, `X` wins ties and
near-ties.

---

## Live snapshot (May 2026)

Comparison of every path for a single-cycle AED → BTC purchase, using
live prices at the moment of this writing:

| Route | 500 AED yields | 25,000 AED yields | Notes |
|---|---|---|---|
| **OKX 2-hop** | 0.00166007 BTC | 0.08300357 BTC | universal winner |
| OKX direct | 0.00165859 BTC | 0.08292960 BTC | -0.089% |
| BitOasis direct | 0.00165435 BTC | 0.08271746 BTC | -0.34% |
| Cross OKX→Binance | 0.00164250 BTC | 0.08302127 BTC | wins above ~25k AED |
| BitOasis 2-hop | 0.00164605 BTC | 0.08230254 BTC | bad: 0.5%×2 takers |

For most AED amounts (small to medium), enable OKX two-hop. For
yearly-bonus bulk buys above ~25k AED, watch the cross-exchange alert
and execute manually.

---

## CLI

```bash
# Audit what the router would do at a specific cycle size
bitcoiners-dca routes --amount 500
bitcoiners-dca routes --amount 25000

# Pair other than the strategy default
bitcoiners-dca routes --amount 1000 --pair BTC/AED
```

---

## Config

```yaml
routing:
  # General
  mode: best_price
  preferred_exchange: bitoasis     # null disables preference
  preferred_bonus_pct: 0.5         # treat preferred as 0.5% cheaper
  exclude_if_spread_pct_above: 2.0

  # Two-hop (same exchange) — default off; enable after audit
  enable_two_hop: false
  intermediates: [USDT]            # could add USDC later

  # Cross-exchange — Telegram alert only, never executes
  enable_cross_exchange_alerts: false
  cross_exchange_min_size_aed: 25000
  cross_exchange_withdrawal_costs:
    USDT: 1.5                      # OKX TRC20 USDT fee
```

---

## When NOT to use multi-hop

- **You manually arrange your AED across exchanges.** If you keep AED on
  the exchange you want to buy from, single-hop direct is simpler and
  has fewer failure modes.
- **You don't trust two-orders-in-sequence execution.** If hop 1
  succeeds and hop 2 fails (rare but possible), you'll have USDT sitting
  on the source exchange. The bot surfaces this loudly via Telegram, but
  manual reconciliation is required.
- **You're cycling tiny amounts** (<100 AED). The 0.09% advantage isn't
  worth two API calls.
