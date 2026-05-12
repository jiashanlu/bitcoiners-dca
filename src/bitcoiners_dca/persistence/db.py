"""
SQLite persistence for trade log + arbitrage alerts + audit trail.

Schema is deliberately simple — append-only event log + materialized views
(computed on query). This makes backups trivial and recovery debug-able.

Tables:
- trades        : every executed buy + sell + withdrawal
- arbitrage_log : every detected arbitrage opportunity (whether acted on or not)
- cycle_log     : one row per DCA cycle (success or fail)
- meta          : key-value config + state
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional, Any

from bitcoiners_dca.core.models import Order, Withdrawal, ArbitrageOpportunity
from bitcoiners_dca.core.strategy import ExecutionResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    exchange TEXT NOT NULL,
    order_id TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    amount_quote TEXT NOT NULL,
    amount_base TEXT,
    price_avg TEXT,
    fee_quote TEXT,
    status TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE(exchange, order_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_exchange ON trades(exchange);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    exchange TEXT NOT NULL,
    withdrawal_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount TEXT NOT NULL,
    address TEXT NOT NULL,
    fee TEXT,
    status TEXT NOT NULL,
    txid TEXT,
    raw_json TEXT,
    UNIQUE(exchange, withdrawal_id)
);

CREATE TABLE IF NOT EXISTS arbitrage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    pair TEXT NOT NULL,
    cheap_exchange TEXT NOT NULL,
    cheap_ask TEXT NOT NULL,
    expensive_exchange TEXT NOT NULL,
    expensive_bid TEXT NOT NULL,
    gross_spread_pct TEXT NOT NULL,
    net_profit_pct TEXT NOT NULL,
    alerted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_arb_timestamp ON arbitrage_log(timestamp);

CREATE TABLE IF NOT EXISTS cycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    intended_amount_aed TEXT,
    overlay_applied TEXT,
    chosen_exchange TEXT,
    order_id TEXT,
    success INTEGER,
    notes TEXT,
    errors TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path = "./data/dca.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL: allow daemon (writer) + dashboard (reader) to run concurrently
        # without "database is locked" stalls. busy_timeout: queue waits 5s
        # instead of failing fast. check_same_thread=False: FastAPI's async
        # threadpool may dispatch reads from different threads.
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit BEGIN/COMMIT in writes
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def record_trade(self, order: Order) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO trades
               (timestamp, exchange, order_id, pair, side, amount_quote,
                amount_base, price_avg, fee_quote, status, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.created_at.isoformat(),
                order.exchange, order.order_id, order.pair, order.side.value,
                str(order.amount_quote),
                str(order.amount_base) if order.amount_base else None,
                str(order.price_filled_avg) if order.price_filled_avg else None,
                str(order.fee_quote),
                order.status.value,
                order.model_dump_json(),
            ),
        )
        self._conn.commit()

    def record_withdrawal(self, w: Withdrawal) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO withdrawals
               (timestamp, exchange, withdrawal_id, asset, amount, address, fee, status, txid, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                w.created_at.isoformat(),
                w.exchange, w.withdrawal_id, w.asset,
                str(w.amount), w.address, str(w.fee), w.status.value,
                w.txid, w.model_dump_json(),
            ),
        )
        self._conn.commit()

    def record_arbitrage(self, opp: ArbitrageOpportunity, alerted: bool = False) -> None:
        self._conn.execute(
            """INSERT INTO arbitrage_log
               (timestamp, pair, cheap_exchange, cheap_ask, expensive_exchange,
                expensive_bid, gross_spread_pct, net_profit_pct, alerted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.timestamp.isoformat(),
                opp.pair,
                opp.cheap_exchange, str(opp.cheap_ask),
                opp.expensive_exchange, str(opp.expensive_bid),
                str(opp.spread_pct), str(opp.net_profit_pct_after_fees),
                1 if alerted else 0,
            ),
        )
        self._conn.commit()

    def record_cycle(self, result: ExecutionResult) -> None:
        self._conn.execute(
            """INSERT INTO cycle_log
               (timestamp, intended_amount_aed, overlay_applied, chosen_exchange,
                order_id, success, notes, errors)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.timestamp.isoformat(),
                str(result.intended_amount_aed),
                result.overlay_applied,
                (
                    result.routing_decision.chosen.route.hops[-1].exchange
                    if result.routing_decision else None
                ),
                result.order.order_id if result.order else None,
                1 if (result.order and not result.errors) else 0,
                json.dumps(result.notes),
                json.dumps(result.errors),
            ),
        )
        if result.order:
            self.record_trade(result.order)
        self._conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, datetime.utcnow().isoformat()),
        )
        self._conn.commit()

    def total_btc_bought(self) -> Decimal:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_base AS REAL)), 0) FROM trades WHERE side='buy' AND status='filled'"
        )
        return Decimal(str(cur.fetchone()[0]))

    def total_aed_spent(self) -> Decimal:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_quote AS REAL)), 0) FROM trades WHERE side='buy' AND status='filled'"
        )
        return Decimal(str(cur.fetchone()[0]))

    def close(self) -> None:
        self._conn.close()
