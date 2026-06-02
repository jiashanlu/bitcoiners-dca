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

CREATE TABLE IF NOT EXISTS withdrawal_destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    address TEXT NOT NULL,
    network TEXT NOT NULL DEFAULT 'bitcoin',
    label TEXT,
    -- manual | binance_whitelist  (auto_withdraw was retired; legacy rows may remain)
    source TEXT NOT NULL DEFAULT 'manual',
    first_used_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    UNIQUE(exchange, address, network)
);
CREATE INDEX IF NOT EXISTS idx_destinations_ex_last
    ON withdrawal_destinations(exchange, last_used_at DESC);
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
        # Persist the effective fee in QUOTE terms. OKX returns fees
        # in BASE (BTC) for AED-quoted buys → raw fee_quote is 0 →
        # tax CSV silently lost the fee on every cycle. effective_fee_quote
        # converts via fee_base × price_filled_avg when needed so the
        # column always carries an AED number. raw_json still has both
        # fields for full fidelity. Audit follow-up 2026-05-24.
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
                str(order.effective_fee_quote),
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
        """Idempotency check for withdrawals.

        Returns True if a withdrawal row exists for (exchange, asset) within
        the last `since_minutes`. Used by any future re-enablement of the
        auto-withdraw daemon path and by tests; today's manual flow doesn't
        consult this gate.

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
        # BEGIN IMMEDIATE takes the write lock at statement 1 rather than
        # lazily on first write, so a concurrent writer (FastAPI threadpool
        # sharing this connection) can't interleave between the cycle_log
        # insert and the per-hop trade inserts and break cycle atomicity
        # (audit 2026-06-02 P3).
        self._conn.execute("BEGIN IMMEDIATE")
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
                        # effective_fee_quote, not raw fee_quote: OKX bills the
                        # fee in BASE (BTC) on AED buys, so the raw column is 0
                        # and the tax CSV silently dropped every OKX fee. This
                        # is the LIVE scheduler path (record_cycle persists each
                        # hop) — record_trade above was fixed in the 2026-05-24
                        # audit but this path was missed. Audit follow-up 2026-05-29.
                        str(o.effective_fee_quote),
                        o.status.value,
                        o.model_dump_json(),
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def record_destination(
        self,
        exchange: str,
        address: str,
        network: str = "bitcoin",
        label: Optional[str] = None,
        source: str = "manual",
    ) -> None:
        """Upsert a withdrawal destination — bumps last_used_at on duplicates.

        Source tags where the address came from:
          - 'manual'            user pasted into the Withdraw-now form
          - 'binance_whitelist' pulled from Binance's whitelist API
        Legacy rows tagged 'auto_withdraw' may exist; auto-withdraw was
        retired from the product surface.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO withdrawal_destinations
                 (exchange, address, network, label, source,
                  first_used_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(exchange, address, network) DO UPDATE SET
                 last_used_at = excluded.last_used_at,
                 label = COALESCE(excluded.label, label),
                 -- Don't downgrade a whitelist-sourced row to manual.
                 source = CASE
                   WHEN source = 'binance_whitelist' THEN source
                   ELSE excluded.source
                 END""",
            (exchange, address, network, label, source, now, now),
        )
        self._conn.commit()

    def list_destinations(self, exchange: str, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            """SELECT exchange, address, network, label, source,
                      first_used_at, last_used_at
               FROM withdrawal_destinations
               WHERE exchange = ?
               ORDER BY last_used_at DESC
               LIMIT ?""",
            (exchange, limit),
        )
        return [dict(row) for row in cur.fetchall()]

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

    # Cross-process cycle lock (audit 2026-06-02 #12). The scheduler daemon
    # and the dashboard Buy-Now run as SEPARATE processes on the same SQLite
    # DB; the daemon's in-process `_cycle_in_progress` flag and the buy-now's
    # `started_at` guard don't cross the boundary, so both could read the
    # same daily_spend in a cycle's fill window and each proceed, overspending
    # max_daily_aed by up to one cycle. This advisory lock serialises them.
    CYCLE_LOCK_META_KEY = "cycle_lock_at"

    def try_acquire_cycle_lock(self, ttl_seconds: int = 900) -> bool:
        """Acquire the cross-process cycle lock. Only one DCA cycle (cron or
        Buy-Now) may hold it at a time. Self-expires after ttl_seconds so a
        crashed cycle can't wedge it (default 900s > the 600s maker timeout).

        Returns True if acquired — caller MUST call release_cycle_lock() when
        the cycle finishes (use try/finally). False if another cycle holds it.
        """
        now = datetime.now(timezone.utc)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (self.CYCLE_LOCK_META_KEY,)
            ).fetchone()
            held = (row["value"] if row else "") or ""
            if held:
                try:
                    age = (now - datetime.fromisoformat(held)).total_seconds()
                except ValueError:
                    age = ttl_seconds + 1  # unparseable → treat as stale
                if age < ttl_seconds:
                    self._conn.execute("ROLLBACK")
                    return False
            self._conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (self.CYCLE_LOCK_META_KEY, now.isoformat(), now.isoformat()),
            )
            self._conn.execute("COMMIT")
            return True
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def release_cycle_lock(self) -> None:
        """Release the cross-process cycle lock (idempotent)."""
        self.set_meta(self.CYCLE_LOCK_META_KEY, "")

    def _sum_decimal(self, column: str, where: str, params: tuple = ()) -> Decimal:
        """Sum a money column EXACTLY with Decimal.

        Money is stored as TEXT (str(Decimal)). Summing via SQLite's
        `CAST(... AS REAL)` coerces to float and reintroduces the binary
        rounding error Decimal exists to avoid, diverging from the exact-
        Decimal tax CSV (audit 2026-06-02 P3). `column`/`where` are static,
        code-controlled strings — never user input.
        """
        cur = self._conn.execute(
            f"SELECT {column} FROM trades WHERE {where}", params
        )
        total = Decimal(0)
        for (val,) in cur.fetchall():
            if val is not None and val != "":
                total += Decimal(str(val))
        return total

    def total_btc_bought(self) -> Decimal:
        # Only count BTC-receiving trades. Multi-hop AED→USDT→BTC routes
        # persist two rows: USDT/AED (amount_base = USDT) and BTC/USDT
        # (amount_base = BTC). Summing both would treat USDT amounts as
        # BTC and produce nonsense totals (1003 "BTC" from 12 cycles).
        return self._sum_decimal(
            "amount_base",
            "side='buy' AND status='filled' AND pair LIKE 'BTC/%'",
        )

    def total_aed_spent(self) -> Decimal:
        # Only count the AED-spending leg of each cycle: either a direct
        # BTC/AED trade or the USDT/AED hop of a multi-hop route. Without
        # this filter, two-hop cycles double-count (AED spent on USDT,
        # then USDT spent on BTC — both denominated and summed).
        return self._sum_decimal(
            "amount_quote",
            "side='buy' AND status='filled' AND pair LIKE '%/AED'",
        )

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
        direct_aed = self._sum_decimal(
            "amount_quote", "side='buy' AND status='filled' AND pair = 'BTC/AED'"
        )

        # Bot's USDT/AED pool.
        usdt_aed_spent = self._sum_decimal(
            "amount_quote", "side='buy' AND status='filled' AND pair='USDT/AED'"
        )
        usdt_bought = self._sum_decimal(
            "amount_base", "side='buy' AND status='filled' AND pair='USDT/AED'"
        )

        # USDT consumed buying BTC.
        usdt_spent_on_btc = self._sum_decimal(
            "amount_quote", "side='buy' AND status='filled' AND pair='BTC/USDT'"
        )

        if usdt_bought > 0 and usdt_spent_on_btc > 0:
            weighted_rate = usdt_aed_spent / usdt_bought
            via_usdt_aed = usdt_spent_on_btc * weighted_rate
        else:
            # No bot-tracked USDT/AED buys, OR no BTC/USDT trades —
            # nothing to attribute via the USDT channel.
            via_usdt_aed = Decimal(0)

        return direct_aed + via_usdt_aed

    # ─── Read helpers used by the dashboard ─────────────────────────────
    #
    # Centralised so dashboard.py doesn't reach into `db._conn.execute(...)`
    # for ad-hoc queries. Any schema rename now breaks compilation here
    # instead of failing silently at request time.

    def recent_filled_buys(self, limit: int = 24) -> list[sqlite3.Row]:
        """Most recent N filled BUY trades (descending by timestamp).
        Includes both BTC-receiving legs and intermediate USDT/AED legs;
        callers that need only one or the other should filter."""
        return self._conn.execute(
            """SELECT timestamp, exchange, pair, side, amount_quote,
                      amount_base, price_avg, status, order_id
               FROM trades
               WHERE side='buy' AND status='filled'
               ORDER BY timestamp DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()

    def alerted_arbitrage_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM arbitrage_log WHERE alerted=1"
        ).fetchone()
        return int(row[0]) if row else 0

    def cycle_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM cycle_log").fetchone()
        return int(row[0]) if row else 0

    def trade_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()
