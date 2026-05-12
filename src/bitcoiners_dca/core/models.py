"""
Domain models — exchange-agnostic data structures used throughout the bot.

Every exchange adapter normalizes its responses to these types. Strategy,
router, arbitrage, and reporting all consume these — never raw exchange JSON.
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# === MONEY ===

class Ticker(BaseModel):
    """Snapshot price + spread for a trading pair on one exchange."""
    exchange: str
    pair: str                  # canonical pair, e.g. "BTC/AED"
    bid: Decimal               # highest buy price
    ask: Decimal               # lowest sell price
    last: Decimal              # last trade price
    mid: Decimal               # (bid + ask) / 2 — convenience
    spread_pct: Decimal        # ((ask - bid) / mid) * 100
    timestamp: datetime

    @classmethod
    def from_prices(cls, exchange: str, pair: str, bid: Decimal, ask: Decimal,
                    last: Optional[Decimal] = None, ts: Optional[datetime] = None) -> "Ticker":
        mid = (bid + ask) / Decimal(2)
        spread_pct = ((ask - bid) / mid * Decimal(100)) if mid > 0 else Decimal(0)
        return cls(
            exchange=exchange, pair=pair, bid=bid, ask=ask,
            last=last or mid, mid=mid, spread_pct=spread_pct,
            timestamp=ts or datetime.utcnow(),
        )


class Balance(BaseModel):
    """Free + total holdings for one asset on one exchange."""
    exchange: str
    asset: str                 # "AED", "BTC", etc.
    free: Decimal              # available to trade
    used: Decimal              # locked in open orders
    total: Decimal             # free + used


# === ORDERS ===

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Order(BaseModel):
    """An order placed (or attempted) on an exchange."""
    exchange: str
    order_id: str              # exchange-issued ID
    pair: str
    side: OrderSide
    type: OrderType
    amount_quote: Decimal      # e.g., AED amount we wanted to spend
    amount_base: Optional[Decimal] = None   # e.g., BTC bought
    price_filled_avg: Optional[Decimal] = None
    fee_quote: Decimal = Decimal(0)         # fee in quote currency (AED)
    fee_base: Decimal = Decimal(0)          # fee in base currency (BTC)
    status: OrderStatus
    created_at: datetime
    filled_at: Optional[datetime] = None


# === WITHDRAWALS ===

class WithdrawalStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class Withdrawal(BaseModel):
    exchange: str
    withdrawal_id: str
    asset: str
    amount: Decimal
    address: str
    fee: Decimal
    status: WithdrawalStatus
    txid: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


# === FEES ===

class FeeSchedule(BaseModel):
    """An exchange's fee structure for a given pair."""
    exchange: str
    pair: str
    maker_pct: Decimal         # e.g. 0.001 for 0.1%
    taker_pct: Decimal
    withdrawal_fee_btc: Decimal  # flat fee in BTC for withdrawal


# === ARBITRAGE ===

class ArbitrageOpportunity(BaseModel):
    """A detected price gap between two exchanges for the same pair."""
    pair: str
    cheap_exchange: str
    cheap_ask: Decimal
    expensive_exchange: str
    expensive_bid: Decimal
    spread_pct: Decimal        # ((expensive_bid - cheap_ask) / cheap_ask) * 100
    net_profit_pct_after_fees: Decimal  # after estimated buy+sell+withdraw fees
    timestamp: datetime
