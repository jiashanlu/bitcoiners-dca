"""
Notifications — Telegram + email backends.

Used by both the strategy engine (trade confirmations, errors) and the
arbitrage monitor (opportunity alerts).
"""
from __future__ import annotations
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

from bitcoiners_dca.core.models import ArbitrageOpportunity
from bitcoiners_dca.core.strategy import ExecutionResult
from bitcoiners_dca.utils.config import NotificationsConfig


class Notifier:
    def __init__(self, config: NotificationsConfig):
        self.config = config

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
        msg = (
            f"✅ *DCA cycle executed*\n\n"
            f"*Amount:* AED {result.intended_amount_aed}"
        )
        if result.overlay_applied:
            msg += f" (overlay: {result.overlay_applied})"
        msg += (
            f"\n*Exchange:* {order.exchange}\n"
            f"*Bought:* {order.amount_base or '?'} BTC "
            f"@ AED {order.price_filled_avg or '?'}/BTC\n"
            f"*Fee:* AED {order.fee_quote}\n"
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
                msg += (
                    f"\n\n💡 Saved {premium:.2f}% vs "
                    f"{result.routing_decision.best_alt.route.label}"
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
        token = os.environ.get(self.config.telegram.bot_token_env)
        chat_id = self.config.telegram.chat_id
        if not token or not chat_id:
            log.debug("telegram notify skipped: token or chat_id missing")
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
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
