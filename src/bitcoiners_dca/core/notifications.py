"""
Notifications — Telegram + email backends.

Used by both the strategy engine (trade confirmations, errors) and the
arbitrage monitor (opportunity alerts).
"""
from __future__ import annotations
import logging
import os
from decimal import Decimal
from typing import Optional

import httpx

log = logging.getLogger(__name__)

from bitcoiners_dca.core.models import ArbitrageOpportunity, Order
from bitcoiners_dca.core.strategy import ExecutionResult
from bitcoiners_dca.utils.config import NotificationsConfig


def _fmt_dec(value: Decimal, max_dp: int = 8) -> str:
    """Render a Decimal as plain text — no scientific notation. Trims
    trailing zeros after the decimal point. Tiny values stay readable
    (0.00000035 instead of 3.5E-7)."""
    if value == 0:
        return "0"
    quant = Decimal(10) ** -max_dp
    s = format(value.quantize(quant), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _format_fee(order: Order) -> str:
    """Render the order's fee in the right currency + a % of cycle cost.

    Exchanges typically charge the fee in the *base* asset for buys
    (e.g. OKX: fee in BTC, not AED). The previous version of this
    notifier blindly labeled fee_quote as 'AED' and the scientific-
    notation render (3.4824E-7) looked like a 20% fee on a 16 AED
    cycle. The reality: that BTC value times BTC price is ~0.027 AED,
    i.e. a 0.16% taker fee — what you'd expect.

    Returns a string like '0.00000035 BTC (~0.03 AED, 0.16%)' or
    '0.03 AED (0.18%)' depending on which fee field is populated.
    """
    if order.fee_base > 0 and order.price_filled_avg:
        base, _, _ = (order.pair or "BTC/AED").upper().partition("/")
        # Convert to quote terms via spot price for the % readout.
        fee_in_quote = order.fee_base * order.price_filled_avg
        pct = (fee_in_quote / order.amount_quote * Decimal(100)
               ) if order.amount_quote else Decimal(0)
        return (
            f"{_fmt_dec(order.fee_base)} {base} "
            f"(~{_fmt_dec(fee_in_quote, 4)} AED, {_fmt_dec(pct, 3)}%)"
        )
    if order.fee_quote > 0:
        pct = (order.fee_quote / order.amount_quote * Decimal(100)
               ) if order.amount_quote else Decimal(0)
        return f"AED {_fmt_dec(order.fee_quote, 4)} ({_fmt_dec(pct, 3)}%)"
    return "0 (no fee reported)"


def _classify_execution(order: Order) -> str:
    """Human-readable execution type — distinguishes maker vs taker.

    Exchanges classify the FEE RATE based on whether the order crossed
    the spread, NOT based on the order's ``type`` field. A "limit"
    order can still pay the taker rate if its limit price was at-or-
    above the best ask when placed (OKX treats it as an aggressive
    limit). The reverse never happens — a market order always pays
    taker. So:

      - ``order.type == MARKET``         → always taker
      - ``order.type == LIMIT`` + low %  → real maker (passive rest)
      - ``order.type == LIMIT`` + high % → "aggressive limit" → taker

    Threshold picks the gap between known maker/taker rates for the
    pair's quote currency. AED-quoted pairs sit in a ~0.40/0.60 band on
    OKX, stable-quoted sit in ~0.08/0.10. 0.50% and 0.09% are the
    midpoints.
    """
    from bitcoiners_dca.core.models import OrderType
    if order.type == OrderType.MARKET:
        return "Taker (market)"
    fee_q = order.effective_fee_quote
    if not order.amount_quote or order.amount_quote == 0 or fee_q <= 0:
        return "Limit (fee unknown)"
    fee_pct = fee_q / order.amount_quote
    quote = ""
    if order.pair and "/" in order.pair:
        quote = order.pair.split("/")[1].upper()
    threshold = Decimal("0.005") if quote == "AED" else Decimal("0.0009")
    if fee_pct < threshold:
        return "Maker (limit, passive fill)"
    return "Taker (limit crossed spread)"


def _route_taker_fee_pct(route) -> Decimal:
    """Cumulative taker-fee % of a route, compounded across hops.

    A 2-hop route with 0.6% + 0.1% on each hop has an effective fee
    of 1 - (1-0.006)(1-0.001) ≈ 0.7006%, not exactly the sum. The
    difference is small but the compounding model matches what the
    router uses in `effective_price`.
    """
    factor = Decimal(1)
    for hop in route.hops:
        factor *= (Decimal(1) + hop.taker_pct)
    return (factor - Decimal(1)) * Decimal(100)


class Notifier:
    def __init__(
        self,
        config: NotificationsConfig,
        *,
        telegram_token_override: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        """Constructor.

        Pass `telegram_token_override` to skip both the SecretStore +
        env lookup and use a literal token for this Notifier instance
        only. Used by the dashboard's /settings/telegram-test endpoint
        so it doesn't have to mutate process-wide os.environ during a
        live request (which leaked across concurrent handlers — audit
        B-#7 2026-05-21).

        Pass `db_path` (the tenant's SQLite path) so the SecretStore
        lookup reads the SAME database the rest of the process uses.
        Without it, token resolution re-loaded config from a hardcoded
        /app/config/config.yaml guess — in CLI/self-host contexts that
        silently resolved a DIFFERENT (often empty/ghost) SecretStore
        and the dashboard-saved token never applied (audit 2026-06-10 P3).
        """
        self.config = config
        self._token_override = telegram_token_override
        self._db_path = db_path

    def _resolve_telegram_token(self) -> Optional[str]:
        """Look up the Telegram bot token in this order:

          1. SecretStore — what the dashboard's `/settings` form writes.
             Encrypted at rest, lives in the tenant's SQLite DB.
          2. Env var named by config.telegram.bot_token_env (default
             `TG_BOT_TOKEN`) — for operator-provisioned tenants and
             local dev where SecretStore may not be wired up.

        Either path returns the raw token string; both being unset
        returns None and the caller skips silently.
        """
        if self._token_override:
            return self._token_override
        # SecretStore first. Lazy-import + try/except so this module stays
        # usable in contexts without a configured DB (CLI smoke tests,
        # unit tests). Prefer the db_path threaded in at construction;
        # the config-reload guess is the legacy fallback only.
        try:
            from bitcoiners_dca.persistence.secrets import SecretStore
            db_path = self._db_path
            if not db_path:
                from bitcoiners_dca.utils.config import load_config
                cfg_path = os.environ.get("DCA_CONFIG") or "/app/config/config.yaml"
                cfg = load_config(cfg_path)
                db_path = cfg.persistence.db_path
            store = SecretStore(db_path)
            stored = store.get("telegram.bot_token")
            if stored:
                log.debug("telegram token resolved from SecretStore (%s)", db_path)
                return stored
        except Exception as e:  # SecretStore unavailable, DB missing, etc.
            log.debug("telegram token: SecretStore lookup failed: %s", e)
        # Env fallback.
        token = os.environ.get(self.config.telegram.bot_token_env) or None
        if token:
            log.debug(
                "telegram token resolved from env %s",
                self.config.telegram.bot_token_env,
            )
        return token

    async def notify_cycle(self, result: ExecutionResult) -> None:
        """Send a DCA cycle summary to all enabled channels."""
        msg = self._format_cycle_message(result)
        await self._send(msg)

    async def notify_arbitrage(self, opp: ArbitrageOpportunity) -> None:
        """Send an arbitrage opportunity alert."""
        msg = (
            f"⚡ *Arbitrage opportunity detected*\n\n"
            f"*Pair:* {opp.pair}\n"
            f"*Buy on:* {opp.cheap_exchange} @ {opp.cheap_ask}\n"
            f"*Sell on:* {opp.expensive_exchange} @ {opp.expensive_bid}\n"
            f"*Gross spread:* {opp.spread_pct:.2f}%\n"
            f"*Est. net (after fees):* {opp.net_profit_pct_after_fees:.2f}%\n\n"
            f"_Detected at {opp.timestamp.isoformat()}. Manual execution only — "
            f"prices may shift before you act._"
        )
        await self._send(msg)

    async def notify_error(self, subject: str, body: str) -> None:
        await self._send(f"❌ *{subject}*\n\n{body}")

    # === internals ===

    def _format_cycle_message(self, result: ExecutionResult) -> str:
        if result.errors and not result.order:
            return (
                f"❌ *DCA cycle failed*\n\n"
                f"Errors:\n" + "\n".join(f"- {e}" for e in result.errors)
            )
        if not result.order:
            return f"⚠️ *DCA cycle skipped* — no order placed.\n" + "\n".join(result.notes)

        order = result.order
        # Prefer the chosen route's label (e.g. "okx: AED→USDT→BTC"). It
        # already encodes the exchange + every hop, so it replaces the
        # old standalone Exchange line. Fall back to order.exchange for
        # buy-now / non-routed paths.
        route_label = None
        chosen = getattr(getattr(result, "routing_decision", None), "chosen", None)
        if chosen is not None:
            route_label = chosen.route.label
        msg = (
            f"✅ *DCA cycle executed*\n\n"
            f"*Amount:* AED {result.intended_amount_aed}\n"
        )
        if result.overlay_applied:
            msg += f"*Overlay:* {result.overlay_applied}\n"
        # Label the fill price in the order's ACTUAL quote currency. A stable-
        # funded cycle fills on BTC/USDT, so price_filled_avg is in USDT — the
        # old hardcoded "AED" mislabelled it (and was off by the USDT→AED rate).
        base_ccy, _, quote_ccy = order.pair.partition("/")
        base_ccy = base_ccy or "BTC"
        quote_ccy = quote_ccy or "AED"
        msg += (
            f"*Route:* {route_label or order.exchange}\n"
            f"*Type:* {_classify_execution(order)}\n"
            f"*Bought:* {_fmt_dec(order.amount_base) if order.amount_base else '?'} {base_ccy} "
            f"@ {quote_ccy} {_fmt_dec(order.price_filled_avg, 2) if order.price_filled_avg else '?'}/{base_ccy}\n"
            f"*Fee:* {_format_fee(order)}\n"
            f"*Order ID:* `{order.order_id}`"
        )
        if result.withdrew_btc:
            msg += (
                f"\n\n💼 Auto-withdrew {result.withdrew_btc} BTC to "
                f"`{(result.withdrew_to_address or '')[:20]}...`"
            )
        if result.routing_decision and result.routing_decision.best_alt:
            premium = result.routing_decision.price_premium_vs_alt_pct()
            if premium > 0:
                chosen_fee = _route_taker_fee_pct(result.routing_decision.chosen.route)
                alt_fee = _route_taker_fee_pct(result.routing_decision.best_alt.route)
                msg += (
                    f"\n\n💡 Saved {premium:.2f}% vs "
                    f"{result.routing_decision.best_alt.route.label}"
                    f"\n_(est. fees taker: chosen "
                    f"{_fmt_dec(chosen_fee, 2)}%, alt {_fmt_dec(alt_fee, 2)}%)_"
                )
        if result.errors:
            msg += "\n\n⚠️ Non-fatal warnings:\n" + "\n".join(f"- {e}" for e in result.errors)
        return msg

    async def _send(self, text: str) -> None:
        if self.config.telegram.enabled:
            await self._send_telegram(text)
        if self.config.email.enabled:
            await self._send_email(text)

    async def _send_telegram(self, text: str) -> None:
        token = self._resolve_telegram_token()
        chat_id = self.config.telegram.chat_id
        if not token or not chat_id:
            log.debug("telegram notify skipped: token or chat_id missing")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code == 400:
                    # Markdown parse failure — dynamic fragments (exchange
                    # errors, route labels, order ids) can contain _ * ` [
                    # which break Telegram's parser, and a dropped 400 ate
                    # exactly the failure alerts the customer most needed
                    # (audit 2026-06-10 P2). Resend as plain text: ugly
                    # formatting beats no alert.
                    log.warning(
                        "telegram 400 (likely Markdown parse) — retrying "
                        "without parse_mode: %s", resp.text[:200],
                    )
                    resp = await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": text,
                            "disable_web_page_preview": True,
                        },
                    )
                if resp.status_code >= 300:
                    log.warning(
                        "telegram notify non-2xx status=%s body=%s",
                        resp.status_code, resp.text[:200],
                    )
            except Exception as e:
                # Notifications must never break trade execution. Log so the
                # operator can see what's wrong, but swallow the exception.
                log.warning("telegram notify failed: %s", e)

    async def _send_email(self, text: str) -> None:
        """Send via SMTP with STARTTLS. Runs in a thread — smtplib is sync."""
        import asyncio
        import smtplib
        from email.message import EmailMessage

        ec = self.config.email
        host, sender, recipient = ec.smtp_host, ec.from_addr, ec.to_addr
        password = os.environ.get(ec.password_env)
        if not (host and sender and recipient and password):
            log.debug("email notify skipped: host/from/to/password incomplete")
            return

        msg = EmailMessage()
        msg["Subject"] = "Bitcoiners DCA — notification"
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content(text)

        def _send_sync() -> None:
            with smtplib.SMTP(host, ec.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(sender, password)
                smtp.send_message(msg)

        try:
            await asyncio.to_thread(_send_sync)
        except Exception as e:
            log.warning("email notify failed: %s", e)


# === Admin ops alert — fires to the operator (Ben), NOT the customer ===
#
# Driven by two env vars set at the tenant / host level (not in customer
# config.yaml). When unset, the function is a no-op — safe to call
# unconditionally from anywhere in the bot.
#
# Why a module-level fire-and-forget function instead of a class:
# the bot's user-facing Notifier is wired to the customer's TG_BOT_TOKEN
# and chat_id. Admin alerts go to a DIFFERENT chat (Ben's) via a
# DIFFERENT bot. Mixing the two channels would either spam the customer
# with ops noise or hide the alert from Ben. Separate concerns,
# separate code path.

def send_admin_alert(text: str, *, tag: str = "ops") -> None:
    """DM the operator's admin Telegram chat. Synchronous + fire-and-forget.

    Reads `ADMIN_TG_BOT_TOKEN` and `ADMIN_TG_CHAT_ID` from env. If either
    is unset, logs at debug and returns — caller does not need to gate.
    The bot+chat are distinct from the customer's Telegram channel
    (TG_BOT_TOKEN / NotificationsConfig.telegram.chat_id) so that ops
    alerts go to Ben without surfacing to the customer.

    Errors swallowed: notifications must NEVER break trade execution.
    """
    token = os.environ.get("ADMIN_TG_BOT_TOKEN")
    chat_id = os.environ.get("ADMIN_TG_CHAT_ID")
    if not token or not chat_id:
        log.debug("admin alert skipped: ADMIN_TG_BOT_TOKEN/CHAT_ID unset")
        return
    # Prepend a TAG + tenant hint so Ben can triage at a glance across
    # multiple tenants reporting to the same DM.
    tenant_hint = os.environ.get("TENANT_ID") or os.environ.get("HOSTNAME") or "?"
    msg = f"🚨 *{tag}* [{tenant_hint}]\n{text}"
    try:
        # httpx.Client (sync) — keeps this callable from non-async hooks
        # like RiskManager.pause(). Short timeout; the bot keeps running
        # even if Telegram is slow.
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            if resp.status_code >= 300:
                log.warning("admin alert non-2xx: %s %s",
                            resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("admin alert failed: %s", e)
