"""
Reports — generate CSV exports from the trade log.

UAE-tax-CSV columns are chosen to be useful both for personal record-keeping
(if the FTA ever asks) and for handover to a UAE accountant. They aren't a
mandated format — FTA hasn't published one for crypto.

Output columns:
  date, exchange, pair, side, amount_aed, amount_btc,
  price_aed_per_btc, fee_aed, source_doc_id

Plus summary block at the bottom with totals.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from bitcoiners_dca.persistence.db import Database

logger = logging.getLogger(__name__)


def export_uae_tax_csv(
    db: Database,
    out_dir: str | Path,
    year: Optional[int] = None,
) -> Path:
    """Write a YYYY.csv to out_dir summarizing all trades that year.

    If year is None, exports lifetime.

    Returns the path written.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if year:
        start = datetime(year, 1, 1)
        end = datetime(year + 1, 1, 1)
        filename = f"bitcoiners-dca-trades-{year}.csv"
    else:
        start = datetime(1970, 1, 1)
        end = datetime.now(timezone.utc) + timedelta(days=1)
        filename = "bitcoiners-dca-trades-lifetime.csv"

    file_path = out_path / filename

    # Pull rows from SQLite directly — easier than reconstructing Pydantic models
    rows = db._conn.execute(
        """SELECT timestamp, exchange, order_id, pair, side,
                  amount_quote, amount_base, price_avg, fee_quote
           FROM trades
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY timestamp ASC""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    total_aed = Decimal(0)
    total_btc = Decimal(0)
    total_fees = Decimal(0)
    total_cost_basis_aed = Decimal(0)

    # Per-stablecoin weighted <stable>/AED rate, sourced from the SHARED
    # helper so the CSV and Database.btc_cost_basis_aed can never drift.
    # USDT and USDC keep SEPARATE rates (average-cost, per asset). Used both
    # to convert a multi-hop BTC/<stable> leg's fee (denominated in the
    # stablecoin) into AED for the fee TOTAL — otherwise every multi-hop
    # cycle under-reported fees by the whole BTC-purchase leg (audit
    # 2026-06-02 P2) — AND to give each BTC/<stable> row a true AED cost
    # basis (task #210). rate[ccy] = AED spent on ccy / units of ccy bought.
    #
    # Note: the helper weights over the WHOLE trade history, not just this
    # report's date window. That's the right denominator for cost basis —
    # the AED a unit of USDT cost doesn't change because of which tax year
    # you slice — and it matches what btc_cost_basis_aed reports.
    _stable_aed = db.stable_aed_rates()

    def _stable_rate(ccy: str) -> Optional[Decimal]:
        """AED-per-unit rate for a stablecoin, or None if it's AED / unknown."""
        if ccy == "AED":
            return Decimal(1)
        return _stable_aed.get(ccy)

    def _fee_to_aed(fee_val: Decimal, fee_ccy: str) -> Optional[Decimal]:
        rate = _stable_rate(fee_ccy)
        if rate is None:
            return None  # no rate available — can't convert
        return fee_val * rate

    with file_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Date",
                "Exchange",
                "Pair",
                "Side",
                "Amount (AED)",
                "Amount (BTC)",
                "Price (AED per BTC)",
                "Fee",
                "Fee Ccy",
                "AED Cost Basis",
                "Order ID",
            ]
        )

        for row in rows:
            ts_str = row["timestamp"].split("T")[0]
            amount_aed = Decimal(str(row["amount_quote"] or 0))
            amount_btc = Decimal(str(row["amount_base"] or 0))
            price = Decimal(str(row["price_avg"] or 0))
            fee = Decimal(str(row["fee_quote"] or 0))
            # The stored fee is in the pair's QUOTE currency (effective_fee_quote):
            # AED for a direct/USDT-AED leg, USDT for a BTC/USDT leg. Label it
            # honestly rather than under a flat "Fee (AED)" header (audit P2).
            pair = str(row["pair"] or "")
            fee_ccy = pair.split("/")[1] if "/" in pair else "AED"

            # Per-row AED cost basis for the BTC-receiving legs (task #210).
            # BTC/AED: the AED outlay itself. BTC/<stable>: the stablecoin
            # amount converted at that stablecoin's OWN weighted AED rate
            # (USDC never borrows USDT's rate). Non-BTC legs (a USDT/AED
            # pre-buy) and stablecoins with no AED history get a blank cell.
            row_cost_basis: Optional[Decimal] = None
            if row["side"] == "buy" and pair.startswith("BTC/"):
                if pair == "BTC/AED":
                    row_cost_basis = amount_aed
                else:
                    quote_rate = _stable_rate(fee_ccy)
                    if quote_rate is not None:
                        row_cost_basis = amount_aed * quote_rate

            writer.writerow(
                [
                    ts_str,
                    row["exchange"],
                    row["pair"],
                    row["side"],
                    f"{amount_aed:.2f}",
                    f"{amount_btc:.8f}",
                    f"{price:.2f}",
                    f"{fee:.8f}".rstrip("0").rstrip(".") or "0",
                    fee_ccy,
                    f"{row_cost_basis:.2f}" if row_cost_basis is not None else "",
                    row["order_id"],
                ]
            )

            if row["side"] == "buy":
                # Only the AED-quoted legs count toward "AED spent" —
                # otherwise multi-hop cycles double-count (AED→USDT→BTC
                # contributes both the AED outlay AND the USDT amount-as-
                # AED). Only the BTC-receiving legs count toward "BTC
                # acquired" — otherwise USDT amount_base values get
                # summed in as if they were BTC, yielding nonsense like
                # 1003 "BTC" from 12 cycles. Fees are AED-equivalent so
                # we only sum the AED-quoted ones to avoid mixing USDT
                # fees in. This matches Database.total_btc_bought /
                # total_aed_spent semantics; see also btc_cost_basis_aed
                # for the weighted-USDT-rate cost-basis calculation.
                if pair.endswith("/AED"):
                    total_aed += amount_aed
                # Sum fees on ALL buy legs, converting non-AED leg fees to AED
                # at the weighted rate. Previously only /AED legs counted, so a
                # multi-hop cycle's BTC/USDT fee (the real Bitcoin-purchase fee)
                # was dropped from the total (audit 2026-06-02 P2).
                fee_aed = _fee_to_aed(fee, fee_ccy)
                if fee_aed is not None:
                    total_fees += fee_aed
                if pair.startswith("BTC/"):
                    total_btc += amount_btc
                if row_cost_basis is not None:
                    total_cost_basis_aed += row_cost_basis

        # Summary section
        writer.writerow([])
        writer.writerow(["=== SUMMARY ==="])
        writer.writerow(["Total AED spent (buys)", f"{total_aed:.2f}"])
        writer.writerow(["Total BTC acquired", f"{total_btc:.8f}"])
        # Cost basis of the BTC actually acquired — excludes idle stablecoin
        # inventory that "Total AED spent" includes. Mirrors the per-stable
        # average-cost methodology in Database.btc_cost_basis_aed (task #210).
        writer.writerow(["BTC cost basis (AED)", f"{total_cost_basis_aed:.2f}"])
        writer.writerow(["Total fees (AED)", f"{total_fees:.2f}"])
        if total_btc > 0:
            avg_price = total_aed / total_btc
            writer.writerow(["Average price (AED/BTC)", f"{avg_price:.2f}"])
        writer.writerow([])
        writer.writerow(
            [
                "Generated by bitcoiners-dca on",
                datetime.now(timezone.utc).isoformat(),
            ]
        )
        writer.writerow(
            [
                "UAE personal Bitcoin gains are not taxed.",
                "This report is for personal record-keeping.",
            ]
        )

    logger.info("Wrote %s", file_path)
    return file_path
