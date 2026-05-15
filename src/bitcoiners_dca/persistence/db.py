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
from datetime import datetime, timedelta, timezone
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

    def recent_withdrawal_exists(
        self, exchange: str, asset: str, since_minutes: int = 60
    ) -> bool:
        """Idempotency check for auto-withdraw.

        Returns True if a withdrawal row exists for (exchange, asset) within
        the last `since_minutes`. Strategy uses this to short-circuit a
        re-attempt after a crash mid-withdraw: even if the exchange's `free`
        balance hasn't decremented yet (rare but possible), we won't fire a
        second `withdraw_btc` call.

        Timestamp comparison is string-lex on ISO format, which works because
        all rows are recorded with tz-aware UTC isoformat (`+00:00` suffix).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        row = self._conn.execute(
            """SELECT 1 FROM withdrawals
               WHERE exchange = ? AND asset = ? AND timestamp >= ?
               LIMIT 1""",
            (exchange, asset, cutoff),
        ).fetchone()
        return row is not None

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
        # One transaction for the whole cycle. The previous implementation
        # commit'd record_trade per hop then commit'd the cycle row at the
        # end — if the daemon was killed between hops (OOM, container
        # recycle, etc.) the DB ended up with partial trades but no
        # cycle_log row, which makes reconciliation impossible. Wrapping
        # in BEGIN/COMMIT means either everything lands or nothing does.
        # Note: connection is opened with isolation_level=None (autocommit)
        # so we explicitly drive the transaction with BEGIN here.
        self._conn.execute("BEGIN")
        try:
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
            # Persist EVERY hop, not just the final BTC-receiving one. For a
            # two-hop AED→USDT→BTC route, this writes 2 rows so the AED leg
            # (the actual stablecoin fee+price) is auditable + reflected in
            # totals. The order_id is the primary key so INSERT OR REPLACE
            # is naturally idempotent per leg.
            for o in result.orders:
                self._conn.execute(
                    """INSERT OR REPLACE INTO trades
                       (timestamp, exchange, order_id, pair, side, amount_quote,
                        amount_base, price_avg, fee_quote, status, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        o.created_at.isoformat(),
                        o.exchange, o.order_id, o.pair, o.side.value,
                        str(o.amount_quote),
                        str(o.amount_base) if o.amount_base else None,
                        str(o.price_filled_avg) if o.price_filled_avg else None,
                        str(o.fee_quote),
                        o.status.value,
                        o.model_dump_json(),
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_meta(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def total_btc_bought(self) -> Decimal:
        # Only count BTC-receiving trades. Multi-hop AED→USDT→BTC routes
        # persist two rows: USDT/AED (amount_base = USDT) and BTC/USDT
        # (amount_base = BTC). Summing both would treat USDT amounts as
        # BTC and produce nonsense totals (1003 "BTC" from 12 cycles).
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_base AS REAL)), 0) "
            "FROM trades WHERE side='buy' AND status='filled' "
            "AND pair LIKE 'BTC/%'"
        )
        return Decimal(str(cur.fetchone()[0]))

    def total_aed_spent(self) -> Decimal:
        # Only count the AED-spending leg of each cycle: either a direct
        # BTC/AED trade or the USDT/AED hop of a multi-hop route. Without
        # this filter, two-hop cycles double-count (AED spent on USDT,
        # then USDT spent on BTC — both denominated and summed).
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_quote AS REAL)), 0) "
            "FROM trades WHERE side='buy' AND status='filled' "
            "AND pair LIKE '%/AED'"
        )
        return Decimal(str(cur.fetchone()[0]))

    def btc_cost_basis_aed(self) -> Decimal:
        """AED cost basis of the BTC the bot has acquired.

        Why this exists: `total_aed_spent` sums every AED outflow,
        including USDT pre-buys for multi-hop routing. When the bot
        carries unused USDT inventory, that inflates the denominator
        and makes avg cost look artificially low vs spot.

        Methodology — approximate-cost-basis:
          - Direct BTC/AED buys: count `amount_quote` (AED) 1:1.
          - BTC/USDT buys: convert `amount_quote` (USDT) → AED at the
            bot's weighted-average USDT/AED purchase rate
            (= total AED spent on USDT / total USDT bought).
          - If bot has no USDT/AED history (e.g. user pre-funded all
            their USDT externally), BTC/USDT trades are excluded — we
            can't fabricate a rate.

        Tradeoffs:
          - If bot bought MORE USDT than it has spent on BTC, the
            leftover USDT inventory is automatically excluded because
            we multiply only `usdt_spent_on_btc` (not the full
            `usdt_aed_spent`).
          - If bot spent MORE USDT on BTC than it bought (because user
            pre-funded USDT externally), we still attribute that excess
            at the weighted rate. Strictly speaking those USDT cost the
            bot nothing, but treating them as "free" produces an
            unrealistically low avg cost — most users imagine those
            USDT had AN AED cost in reality (just incurred outside the
            bot). The weighted-rate approximation matches user
            intuition.

        Returns 0 if no BTC has been bought yet.
        """
        # Direct BTC/AED buys.
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_quote AS REAL)), 0) "
            "FROM trades WHERE side='buy' AND status='filled' "
            "AND pair = 'BTC/AED'"
        )
        direct_aed = Decimal(str(cur.fetchone()[0]))

        # Bot's USDT/AED pool.
        cur = self._conn.execute(
            "SELECT "
            "  COALESCE(SUM(CAST(amount_quote AS REAL)), 0) AS aed_spent, "
            "  COALESCE(SUM(CAST(amount_base AS REAL)), 0)  AS usdt_bought "
            "FROM trades WHERE side='buy' AND status='filled' "
            "AND pair='USDT/AED'"
        )
        row = cur.fetchone()
        usdt_aed_spent = Decimal(str(row[0]))
        usdt_bought    = Decimal(str(row[1]))

        # USDT consumed buying BTC.
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_quote AS REAL)), 0) "
            "FROM trades WHERE side='buy' AND status='filled' "
            "AND pair='BTC/USDT'"
        )
        usdt_spent_on_btc = Decimal(str(cur.fetchone()[0]))

        if usdt_bought > 0 and usdt_spent_on_btc > 0:
            weighted_rate = usdt_aed_spent / usdt_bought
            via_usdt_aed = usdt_spent_on_btc * weighted_rate
        else:
            # No bot-tracked USDT/AED buys, OR no BTC/USDT trades —
            # nothing to attribute via the USDT channel.
            via_usdt_aed = Decimal(0)

        return direct_aed + via_usdt_aed

    def close(self) -> None:
        self._conn.close()
