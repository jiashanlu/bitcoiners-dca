# DCA dashboard — mobile UI audit (Pixel 5, 393×851)

**Captured:** 2026-05-16 via Playwright against `dev-app.bitcoiners.ae/dca/console/*` with Ben's session cookie.
**Spec:** `bitcoiners-app/tests/e2e/prod-mobile-smoke.dca-audit.spec.ts`
**Screenshots:** `bitcoiners-app/mobile-audit/<route>.png`

## TL;DR

The dashboard renders, but the mobile experience has four real problems and a handful of polish nits. The single biggest offender is **data tables overflowing the viewport** (trades is 238px wider than the screen). Touch targets and font sizes are the next tier.

## Findings table

| Route | Overflow | Tiny targets (<36px) | Smallest font | Notes |
|---|---|---|---|---|
| overview | **162 px** | 3 | 10 px | Recent-trades + accumulation chart |
| strategy | 46 px | **18** | 11 px | Form rows + inline help links + checkboxes |
| exchanges | 0 px | 4 | 12 px | 502 on htmx partial |
| balances | 80 px | 2 | 10 px | Per-exchange table |
| prices | 88 px | 2 | 10 px | Bid/ask/spread table |
| trades | **238 px** | 2 | 10 px | Worst — 6-col table in 390 px viewport |
| routes-audit | 159 px | 4 | 10 px | Route comparison table |
| withdrawals | 0 px | **12** | 11 px | Per-exchange form rows |
| backtest | 0 px | 9 | 11 px | Numeric form fields |
| settings | 0 px | 9 | 11 px | License + flags form |

## Plan (per problem cluster)

### P0 — Tables overflow viewport (6 of 10 routes)

The horizontal overflow comes entirely from data tables: trades, balances, prices, routes-audit, recent-trades-on-overview. The 6-column trades table is the worst (238 px past the right edge — that's 60 % of the visible width).

**Fix:** on narrow viewports, swap `<table>` rendering for a stacked-card view of each row. One DCA cycle = one card with label/value pairs. Same data, no horizontal scroll.

Concretely, in `bitcoiners_dca/web/templates/partials/trades_list.html` and the equivalent partial that renders each data table, wrap the existing `<table>` block in `{% if not mobile %}` and add a sibling `{% else %}` block that renders the same rows as `<div class="card">` blocks. The `mobile` flag can be a server-side `request.headers.get("sec-ch-ua-mobile") == "?1"` check (cheap, cached by CF), OR — simpler — a pure-CSS swap using a `@media (max-width: 640px)` rule that hides the table and shows the card list. CSS-only avoids server-side UA sniffing and keeps both layouts available for the same response.

Sample CSS pattern:

```css
@media (max-width: 640px) {
  table.trades-table { display: none; }
  .trades-mobile-cards { display: block; }
}
@media (min-width: 641px) {
  .trades-mobile-cards { display: none; }
}
```

Each card ~80 px tall, 5 rows visible per screen — better than 1 row with 4 directions of scroll.

### P0 — Touch targets < 36 px (4 routes with >8 each)

WCAG 2.5.5 recommends ≥ 24 × 24, Apple HIG ≥ 44 × 44; we're enforcing 36 as midpoint. Strategy page has 18 targets failing — mostly inline help icons (the small `(?)` tooltip triggers next to labels) and form checkboxes that are 16 × 16 native.

**Fix:** add a single base-stylesheet rule that bumps interactive elements:

```css
@media (max-width: 640px) {
  button, a, input[type="checkbox"], input[type="radio"], select {
    min-height: 40px;
  }
  input[type="checkbox"], input[type="radio"] {
    width: 22px;
    height: 22px;
  }
  /* help-icon click targets — visible 16px icon, 36px hit area */
  .help-icon { padding: 10px; }
}
```

### P1 — Sidebar nav stacks vertically + eats 200+ px before content

On every page, the desktop sidebar (Overview / Strategy / Exchanges / …) is rendered at the top as a vertical list, pushing real content down. This is the audit's "feels terrible" cue more than the overflow — the user lands on a screenful of menu before any of their data shows.

**Fix:** collapse the sidebar to a hamburger drawer + page-title header on narrow viewports. Tap the hamburger to open the nav drawer; tap any item to close + navigate. Keep the same nav items, same Jinja partial — just swap the layout shell.

Quickest implementation:

```html
<!-- in _base.html, replace the static <nav> with: -->
<button class="nav-toggle" type="button" aria-controls="nav" aria-expanded="false">☰</button>
<nav id="nav" class="nav nav-collapsed">…existing items…</nav>
<style>
@media (max-width: 640px) {
  .nav { display: none; }
  .nav.is-open { display: block; position: fixed; inset: 0; z-index: 50; }
  .nav-toggle { display: inline-flex; min-height: 44px; }
}
@media (min-width: 641px) { .nav-toggle { display: none; } }
</style>
<script>
  document.querySelector('.nav-toggle')?.addEventListener('click', (e) => {
    const nav = document.querySelector('.nav');
    nav?.classList.toggle('is-open');
    e.currentTarget.setAttribute('aria-expanded', nav?.classList.contains('is-open') ? 'true' : 'false');
  });
</script>
```

### P1 — 10–11 px monospace text in 6 routes

The `.mono` class is `font-size: 11px` which collapses to 10 px after subpixel rounding. 16 px is the mobile-readability floor.

**Fix:** bump `.mono` to `13px` (still differentiates from prose) and `.muted` from `12px` to `14px` on mobile only. One CSS-vars edit in `_base.html`'s `<style>` block.

### P2 — `/strategy` and `/exchanges` 502s

- `/strategy` requests `/dca/console/api/current-btc-aed` and gets 502. This is the live-BTC-price endpoint used to show a per-cycle BTC preview. The page works without it; just a missing data point. Verify the endpoint exists and the proxy can reach it.
- `/exchanges` 502 happens on the htmx partial swap, likely the exchange health-check timing out (each exchange's `health_check()` can take 2–3 s; if more than ~5 s total, the proxy's 25 s timeout is plenty, so this is something else — maybe a Cloudflare edge hiccup).

**Fix:** sample the endpoints over a few minutes to see if it's deterministic or transient. If deterministic, dig into the tenant FastAPI logs. If transient, add a client-side htmx `htmx-target` error fallback that displays "couldn't refresh — try again" instead of leaving the partial in an error-page state.

### P2 — `/exchanges` body background is transparent

`bodyBackground: rgba(0,0,0,0)` on `/exchanges` means the dark theme didn't apply — probably because the page renders a partial (`{% extends %}` outside the base template) or htmx replaced the `<body>` content with a partial that lacks the wrapping `<div class="bg">`.

**Fix:** ensure every full-page route extends `_base.html` and the body retains its background-color rule.

### P3 — `/login/check-email` not covered + no logged-in vs logged-out smoke

The smoke spec doesn't authenticate; this audit spec does, but isn't part of CI. Worth adding a CI step that runs the auth'd audit against the home dev tenant on every push to dev so regressions in mobile layout trip CI.

## Suggested execution order

1. **P0 — table → card on mobile** for trades, balances, prices, overview's "recent trades", routes-audit. CSS-only swap. ~30 min.
2. **P0 — touch-target min-heights** in one mobile media query. ~10 min.
3. **P1 — hamburger nav** (single CSS + 8 LOC JS). ~20 min.
4. **P1 — mono font bump** to 13px / muted to 14px on mobile. ~5 min.
5. **P2 — debug the /strategy and /exchanges 502s** with curl + tenant logs.
6. **P3 — wire the audit spec into CI** as a new Playwright project (`tenant-audit` against dev-app, gated on a service-token CF Access cookie).

If you want me to take this in order, say so and I'll just do it (CSS changes are low-risk on the bot template). If you want to pick and choose, the screenshots in `bitcoiners-app/mobile-audit/*.png` show what's actually broken so you can prioritize.
