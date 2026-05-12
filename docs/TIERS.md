# Tiers — Free vs Pro vs Business

bitcoiners-dca ships under three tiers. The same binary serves all three;
your license tier decides which features actually run.

## Quick comparison

| Capability | Free | Pro | Business |
|---|:---:|:---:|:---:|
| **Core** |
| Recurring DCA buys on schedule | ✓ | ✓ | ✓ |
| Tax CSV export (UAE FTA format) | ✓ | ✓ | ✓ |
| Local web dashboard | ✓ (localhost) | ✓ (hosted) | ✓ (hosted + custom) |
| Backtest engine (CLI) | ✓ | ✓ | ✓ |
| Risk circuit breakers + pause/resume | ✓ | ✓ | ✓ |
| Single-exchange execution | ✓ | ✓ | ✓ |
| On-chain auto-withdraw to hardware wallet | ✓ | ✓ | ✓ |
| Telegram notifications | ✓ | ✓ | ✓ |
| **Pro features** |
| Multi-exchange routing | — | ✓ | ✓ |
| Multi-hop routing (AED → USDT → BTC) | — | ✓ | ✓ |
| Cross-exchange arbitrage alerts | — | ✓ | ✓ |
| Maker-mode execution (limit orders) | — | ✓ | ✓ |
| Buy-the-dip overlay | — | ✓ | ✓ |
| Volatility-weighted DCA | — | ✓ | ✓ |
| Time-of-day optimization | — | ✓ | ✓ |
| Drawdown-aware sizing | — | ✓ | ✓ |
| Funding-rate monitor (basis-trade signals) | — | ✓ | ✓ |
| Lightning auto-withdraw (OKX BOLT11) | — | ✓ | ✓ |
| Email + SMS notifications | — | ✓ | ✓ |
| Hosted dashboard at app.bitcoiners.ae | — | ✓ | ✓ |
| **Business features** |
| Basis-trade execution (long spot + short perp) | — | — | ✓ |
| Covered-call yield via LN Markets | — | — | ✓ |
| Multi-asset DCA (BTC + ETH + SOL with weights) | — | — | ✓ |
| Stablecoin yield on AED queue | — | — | ✓ |
| Tax-loss harvesting | — | — | ✓ |
| Family-office multi-strategy mode | — | — | ✓ |
| 1:1 onboarding call + priority support | — | — | ✓ |
| **Pricing** | AED 0 / month | AED 49 / month | AED 499+ / month |

## How tiering is enforced

Free tier needs no key — install the bot and run it. Pro and Business
require a license key signed by the publisher (bitcoiners.ae). The bot
ships with the publisher's public key hardcoded; it verifies the
license token offline — no phone-home, no telemetry.

Set the tier in `config.yaml`:

```yaml
license:
  tier: pro                       # free | pro | business
  key: "<token from bitcoiners.ae>"
```

If `tier: pro` is set but the key is missing, invalid, or expired, the
bot silently downgrades to free tier and logs a warning. Premium features
the user enabled in config are then disabled automatically — single
source of truth, no scattered checks throughout the code.

Run `bitcoiners-dca license` to inspect which tier is active and which
features it unlocks.

## Why this split (philosophy)

**Free tier is genuinely useful.** It's not a 7-day-trial-with-watermark.
Single-exchange recurring DCA + tax CSV + on-chain auto-withdraw + risk
circuit breakers is a complete product for someone who just wants to
buy 500 AED of BTC every Monday on BitOasis and withdraw to their
Coldcard at 0.01 BTC.

**Pro tier captures the smart-routing alpha** that we documented in
`docs/ROUTING.md`. Multi-exchange routing alone tends to save 0.3-0.5%
on every buy under current UAE market structure. At 20k AED/month, that's
AED 700-1,200/year — about a year's Pro subscription paid for by the
routing alone.

**Business tier is high-touch yield products.** Basis-trade execution
needs careful risk management. LN Markets covered calls need
position-size discipline. Tax-loss harvesting needs jurisdiction-specific
rules. These are operationally intense — Business pricing reflects that
we're managing those concerns for you, not just running software.

## Self-hosting Pro/Business

The license check is intentionally NOT obfuscated. A determined user can
fork the repo and remove the check. We're not trying to win that arms
race. The hosted tier is priced for the convenience of not running
infrastructure yourself, getting alerts, knowing the bot updates as we
ship improvements, and one-line cancellation.

If self-hosting works better for you, we'd rather have you as a happy
free-tier user than an annoyed pirate. If self-hosting Pro/Business
appeals (your own VPS, your own keys never leaving your hardware),
contact us — we'll discuss an honor-system arrangement.

## How to get a key

1. Visit https://bitcoiners.ae/dca-bot
2. Click "Get Pro access"
3. Choose tier + monthly/annual
4. Pay via card or BTC Lightning
5. Receive your license token by email
6. Paste it into `license.key` in `config.yaml`, restart the bot

The token is a base64 string. It does NOT include your API keys. You
can copy it anywhere — losing it doesn't expose your funds.
