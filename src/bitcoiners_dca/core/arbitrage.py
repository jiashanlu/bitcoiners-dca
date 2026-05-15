"""
Arbitrage monitor — detects price gaps across exchanges and emits alerts.

Detection only — does NOT execute trades. Auto-executing arbitrage in the UAE
is regulatorily ambiguous (potentially VASP-licensed activity) and operationally
risky (withdrawal delays kill the spread). Alerting lets the user act manually.

The estimated net profit accounts for:
- Buy-side taker fee (cheap exchange)
- Sell-side taker fee (expensive exchange) — assumed similar to buy-side
- BTC withdrawal fee (cheap → expensive)
- A safety margin for slippage + timing risk
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from bitcoiners_dca.core.models import ArbitrageOpportunity, Ticker, FeeSchedule
from bitcoiners_dca.exchanges.base import Exchange


class ArbitrageMonitor:
    def __init__(
        self,
        min_spread_pct: Decimal = Decimal("1.5"),
        slippage_buffer_pct: Decimal = Decimal("0.3"),
    ):
        """
        Alerts when gross spread between cheap and expensive exchange exceeds min_spread_pct.
        Net-profit calculation also includes fees + slippage buffer to give the user
        a realistic expectation of what they'd actually pocket.
        """
        self.min_spread_pct = min_spread_pct
        self.slippage_buffer_pct = slippage_buffer_pct

    async def detect(
        self,
        exchanges: list[Exchange],
        pair: str = "BTC/AED",
    ) -> list[ArbitrageOpportunity]:
        """Returns list of opportunities sorted by net profit pct, descending."""
        # Fetch ticker + fees from all exchanges in parallel
        tasks = [self._fetch(ex, pair) for ex in exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        quotes: list[tuple[Exchange, Ticker, FeeSchedule]] = [
            r for r in results if not isinstance(r, Exception)
        ]

        opportunities: list[ArbitrageOpportunity] = []

        # Compare every pair of exchanges
        for i, (cheap_ex, cheap_t, cheap_f) in enumerate(quotes):
            for j, (exp_ex, exp_t, exp_f) in enumerate(quotes):
                if i == j:
                    continue

                # Direction: BUY on cheap_ex's ask, SELL on exp_ex's bid
                gross_spread_pct = (
                    (exp_t.bid - cheap_t.ask) / cheap_t.ask * Decimal(100)
                )
                if gross_spread_pct < self.min_spread_pct:
                    continue

                # Net profit estimate
                buy_fee_pct = cheap_f.taker_pct * Decimal(100)
                sell_fee_pct = exp_f.taker_pct * Decimal(100)
                # Withdrawal fee from cheap exchange — approximate as % of trade
                # (small flat amount; trader buys ~1 BTC, withdrawal fee in BTC ~= small %)
                # Conservative estimate: 0.05% for a 1 BTC arb
                withdrawal_fee_pct_approx = Decimal("0.05")

                net_pct = (
                    gross_spread_pct
                    - buy_fee_pct
                    - sell_fee_pct
                    - withdrawal_fee_pct_approx
                    - self.slippage_buffer_pct
                )

                if net_pct <= 0:
                    continue  # fees eat the spread, not a real opportunity

                opportunities.append(ArbitrageOpportunity(
                    pair=pair,
                    cheap_exchange=cheap_ex.name,
                    cheap_ask=cheap_t.ask,
                    expensive_exchange=exp_ex.name,
                    expensive_bid=exp_t.bid,
                    spread_pct=gross_spread_pct,
                    net_profit_pct_after_fees=net_pct,
                    timestamp=datetime.now(timezone.utc),
                ))

        opportunities.sort(key=lambda o: -o.net_profit_pct_after_fees)
        return opportunities

    async def _fetch(self, ex: Exchange, pair: str):
        ticker, fees = await asyncio.gather(ex.get_ticker(pair), ex.get_fee_schedule(pair))
        return (ex, ticker, fees)
