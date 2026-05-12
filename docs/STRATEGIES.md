# Strategy overlays

The base DCA loop says "buy AED N every period". Overlays are pluggable
modifiers that adjust N based on market state. They're composable —
enabled overlays apply in config-declared order, and their multipliers
compound.

```yaml
overlays:
  buy_the_dip:        { enabled: true,  ... }   # Pro+
  volatility_weighted:{ enabled: false, ... }   # Pro+
  time_of_day:        { enabled: false, ... }   # Pro+
  drawdown_aware:     { enabled: false, ... }   # Pro+
```

Free tier ignores all overlays — base cycles only. Each overlay is gated
by the license framework; see `docs/TIERS.md`.

---

## 1. Buy-the-dip

Multiply the buy when BTC has dropped meaningfully in a lookback window.

```yaml
overlays:
  buy_the_dip:
    enabled: true
    threshold_pct: -10      # trigger when price down ≥10% from lookback
    lookback_days: 7
    multiplier: 2.0
```

Math:

```
pct_change = (price_now - price_7d_ago) / price_7d_ago × 100

if pct_change ≤ threshold_pct:
    buy_size = base × multiplier
else:
    buy_size = base
```

**When to use:** if you have spare AED that you'd be willing to deploy
during dips, this overlay does it systematically. Pairs well with a
higher `risk.max_daily_aed` cap so a dip-multiplied buy isn't clamped.

**Caveat:** "down 10% in 7 days" is a noisy signal. In trendier bear
markets you'll trigger on every cycle; in choppy sideways tape you may
never trigger. Tune `threshold_pct` based on your risk appetite — a
deeper `-15%` triggers less often but means buying real drawdowns.

---

## 2. Volatility-weighted DCA

Buy LESS when realized volatility is high, MORE when it's low.

```yaml
overlays:
  volatility_weighted:
    enabled: true
    target_vol_pct: 50      # BTC's rough long-run norm
    slope: 0.02             # sensitivity (per pct-point of vol delta)
    min_factor: 0.25
    max_factor: 2.0
```

Math:

```
factor = clamp(1 + slope × (target_vol - realized_vol_30d),
               min_factor, max_factor)

buy_size = base × factor
```

Examples with defaults (target=50%, slope=0.02):

| realized vol | factor | effect |
|---|---|---|
| 30% | 1.40x | "compression often precedes a move — buy more" |
| 50% | 1.00x | normal |
| 80% | 0.40x | "uncertainty premium — preserve cash" |
| 200% | 0.25x | clamped — extreme regime, minimum buy |

**Theory:** vol-targeting is a well-known equity-portfolio technique.
Applied to DCA, it asymmetrically defers risk when the market is
chaotic and accelerates accumulation during compressed regimes (which
historically precede expansions). It is NOT timing — it's risk
management on top of timing-agnostic DCA.

**Where the data comes from:** the strategy fetches a 30-day price
series from the same source used by the backtest engine (CoinGecko by
default) and computes daily log-returns × √365. The fetch is cached for
1 hour to avoid hammering the API.

**Caveat:** vol regimes change over months. Don't over-tune. The defaults
were picked to roughly halve buys when 30d realized vol is >80% (a
genuine fear-driven regime) and double them when it's <30% (a genuine
calm regime). Most cycles will be near 1.0x.

---

## 3. Time-of-day optimization

UAE-licensed exchanges have lower liquidity at 3-6 AM Dubai time, which
widens bid-ask spreads. Two modes:

```yaml
overlays:
  time_of_day:
    enabled: true
    mode: skip_if_not_best       # or scale_by_spread
    preferred_hours: [9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    spread_scale_min: 0.5
    spread_scale_max: 1.5
```

### skip_if_not_best

If the cycle's hour is not in `preferred_hours`, skip the buy entirely.
The scheduler retries on the next configured time. This is most useful
when paired with `frequency: daily` so missed hours don't mean missed
weeks.

### scale_by_spread

Use observed median spread per hour-of-day to scale the buy. Hours with
tighter-than-average spreads buy MORE; wider hours buy LESS. Clamped
to [spread_scale_min, spread_scale_max] so a wild hour never zeroes the
buy.

This needs spread history per hour, which the bot accumulates from its
`prices` polling over time. The first week or two of operation, the
overlay no-ops because there isn't enough history.

**When to use:** if you run daily DCA but your work schedule means your
cycle time is 3 AM and you can't easily change it. The bot will defer
or down-weight those buys.

**When NOT to use:** if your weekly DCA is already at 9 AM Dubai (the
default in `config.example.yaml`), you're already optimal — this
overlay would mostly no-op.

---

## 4. Drawdown-aware sizing

Multiply the buy when BTC is meaningfully below its all-time high. Bear-
market accumulation tool.

```yaml
overlays:
  drawdown_aware:
    enabled: true
    tiers:
      - {threshold_pct: -20, multiplier: 1.5}
      - {threshold_pct: -40, multiplier: 2.5}
      - {threshold_pct: -60, multiplier: 4.0}
```

Math: compute drawdown = (current_price - ath_price) / ath_price × 100
(a negative number when below ATH). Find the deepest matching tier and
use its multiplier.

| drawdown from ATH | multiplier |
|---|---|
| -10% | 1.0x (no tier matched) |
| -25% | 1.5x (matched -20% tier) |
| -45% | 2.5x (matched -40% tier) |
| -75% | 4.0x (matched -60% tier) |

**Distinct from buy-the-dip:** drawdown is anchored to the cycle ATH
(months/years of context). Dip is anchored to the lookback (7 days
default). They compound — in a March-2020 style crash you'd hit BOTH
overlays simultaneously: 2.5x (drawdown) × 2.0x (dip) = 5x base buy.

**Pair with a high `risk.max_single_buy_aed`** if you want the
compounded multipliers to actually deploy. Otherwise risk-cap clamps
back to the cap regardless of overlay output. That's intentional — the
cap is the hard belt-and-suspenders, the overlays are the throttle.

---

## Composability

Overlay multipliers compound:

```
final_amount = base × buy_the_dip_mult × volatility_mult × drawdown_mult
```

Order matters only in two ways:
1. `time_of_day.mode=skip_if_not_best` short-circuits — if it fires, no
   other overlay matters this cycle.
2. The audit log lists overlays in declared order. Cosmetic.

A worked example with all four enabled, defaults, in a bear-market dip
at 10 AM Dubai when 30d vol is 75% and BTC is -45% from ATH and down
-12% in 7 days:

```
base = 500 AED

buy_the_dip:        (-12% ≤ -10%)  → 2.0x
volatility:         (75 - 50) × 0.02 = 0.5 below target → 0.5x
time_of_day:        (10 in window) → 1.0x (no-op)
drawdown_aware:     (-45% ≤ -40%) → 2.5x

final = 500 × 2.0 × 0.5 × 1.0 × 2.5 = 1250 AED
```

The vol overlay's caution (0.5x) tempers the aggressive dip + drawdown
combo (2.0 × 2.5 = 5.0x → 2.5x net). Risk cap then enforces a hard
upper bound regardless.

---

## Backtesting your overlay stack

```bash
# Backtest a buy-the-dip-only year
bitcoiners-dca backtest --days 365 --dip --dip-threshold -7

# Stack multiple overlays via config
bitcoiners-dca backtest --days 365 --config configs/aggressive-bear.yaml
```

`docs/backtest_recipes.md` (TODO) will have starter configs you can copy.
