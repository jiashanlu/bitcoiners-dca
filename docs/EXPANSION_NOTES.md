# Geographic expansion — research notes

Internal notes on candidate non-UAE markets for v0.6+. NOT customer-facing.

## 🇸🇦 Saudi Arabia

**Verdict: requires more research before shipping an adapter.**

- Rain (rain.bh, KSA + Bahrain licensed) — public API at api.rain.bh
  returns Cloudflare 530 from our test host. Likely needs proper headers
  or browser fingerprint (we hit similar friction on BitOasis docs;
  fetching via Playwright worked). Worth a deeper probe.
- OKX `BTC-SAR` market: NOT listed. OKX UAE serves SAR users from the
  AED market via cross-currency, not a native pair.
- Binance Saudi: announced license under SAMA's regulatory sandbox; not
  yet live as a separate API.

**Strategy for v0.6:** add a Rain adapter once we can hit the public API
with a browser-like client. Rain has BTC/SAR + BTC/USDT pairs. Same
adapter pattern as BitOasis (REST + Bearer auth + Cloudflare-fronted).

## 🇹🇷 Turkey

**Verdict: highest near-term DCA motivation. Ship in v0.6.**

- Binance Turkey (binance.com BTCTRY pair) is LIVE — verified just now:
  bid 3,704,259 TRY / ask 3,704,526 TRY (tight spread). Our existing
  Binance adapter works for BTC/TRY out of the box. Need:
  - Add BTC/TRY to the supported pair list
  - Verify Turkish KYC + tax requirements
  - Add a Turkish-language UI option (Arabic was already on the v0.5
    roadmap — same i18n scaffolding)
- BTCTurk + Paribu are TR-native exchanges (BIST-registered). BTCTurk
  has a public API similar to Binance's; would be a 4-6h adapter port.
- Turkish inflation in 2025-2026 has stayed high — DCA into BTC is a
  particularly compelling pitch here.

**Strategy for v0.6:**
1. Enable `BTC/TRY` in the existing Binance adapter (no new code needed,
   just lift the BTC/USDT-only default)
2. Add a BTCTurk adapter as the local-license-friendly option
3. Launch `bitcoiners.tr` subdomain with TR localization

## 🇦🇷 Argentina

**Verdict: technically interesting, regulatory thinner. Defer to v0.7.**

- No direct BTC/ARS pair on global exchanges (we'd route via USDT).
- Lemon, Belo, Buenbit are local "neobank" style — they wrap USDT/USDC
  but expose minimal APIs. Would likely need browser-automation rather
  than REST.
- Strong product-market fit (Argentine inflation has been the driver of
  retail crypto adoption in LatAm) but operational ceiling is harder.

**Strategy:** revisit when one of the Argentine exchanges publishes a
proper API. For now, ARS users CAN use the bot by:
1. Converting ARS → USDT on their local on-ramp (manually)
2. Running the bot on Binance global with `strategy.pair: BTC/USDT`

Document this path in `docs/MARKETS_BEYOND_UAE.md` (TODO v0.6).

## 🇰🇼 / 🇧🇭 Kuwait / Bahrain

**Verdict: ride on Rain.** Rain serves both. Same adapter, same product.

## 🇸🇬 Singapore / 🇭🇰 Hong Kong

**Verdict: defer.**

- Crypto.com Exchange (Singapore) has a robust public API. Could add a
  Crypto.com adapter for SGD pairs.
- HashKey (HK) is institutional-only.
- Different regulatory shape (MAS vs HKMA) — partner with a local
  compliance reviewer before shipping.

## 🇮🇳 India

**Verdict: skip for now.**

- WazirX + CoinDCX have APIs but India's regulatory churn (1% TDS,
  exchanges in/out of compliance) makes this market a moving target.
- Revisit if/when stable framework appears.

---

## Architecture implications

Each new currency adds:
1. A new exchange adapter (BTCTurk, Rain, Crypto.com SG)
2. A currency-localized default config template
3. A subdomain on bitcoiners.ae (or a fresh domain — `bitcoiners.tr`,
   `bitcoiners.sa`)
4. Optional i18n bundle for the dashboard

The bot's CORE code is already currency-agnostic — every pair is
`<BASE>/<QUOTE>`, every overlay is dimensionless. The work is in the
adapters + localization.

## Sequencing recommendation

**v0.6** (next major): Turkey
- Enable BTC/TRY on Binance adapter
- Add BTCTurk adapter
- Launch `bitcoiners.tr` (or a /tr/ section on bitcoiners.ae) with TR copy

**v0.7**: Basis-trade execution (in regions where funding makes sense)
+ LN Markets sidecar.

**v0.8**: Saudi market — Rain adapter once we've cracked their
Cloudflare layer.

**v0.9**: Multi-asset DCA → Business-tier integration. Multi-strategy
family-office mode.

**v1.0**: Public launch with full English/Arabic/Turkish UI.
