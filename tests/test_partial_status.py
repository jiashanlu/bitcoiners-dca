"""
Regression tests for resolve_partial_status (audit 2026-06-02 P1
partial-status-never-emitted).

Exchanges report a partially-filled-but-resting limit as status 'open'
(filled>0, remaining>0). Every adapter mapped that to PENDING, so
OrderStatus.PARTIAL was never produced and the strategy's partial-fill
handling was dead code — the precondition for the maker_fallback double-buy.
resolve_partial_status derives PARTIAL from the fill quantities.
"""
from __future__ import annotations

from decimal import Decimal

from bitcoiners_dca.core.models import OrderStatus
from bitcoiners_dca.exchanges.base import resolve_partial_status


def test_open_with_partial_fill_becomes_partial():
    # ccxt "open" order, half filled → PARTIAL.
    assert resolve_partial_status(OrderStatus.PENDING, "0.5", "1.0") == OrderStatus.PARTIAL


def test_open_with_zero_fill_stays_pending():
    assert resolve_partial_status(OrderStatus.PENDING, "0", "1.0") == OrderStatus.PENDING
    assert resolve_partial_status(OrderStatus.PENDING, None, "1.0") == OrderStatus.PENDING


def test_open_fully_filled_but_unknown_total_stays_pending():
    # filled>=amount with an open status is ambiguous (settling) — leave it
    # for the closed/FILLED mapping on the next poll, don't guess.
    assert resolve_partial_status(OrderStatus.PENDING, "1.0", "1.0") == OrderStatus.PENDING


def test_filled_with_unknown_amount_becomes_partial():
    # Some shapes omit the total; a non-zero fill on an open order is partial.
    assert resolve_partial_status(OrderStatus.PENDING, "0.3", None) == OrderStatus.PARTIAL
    assert resolve_partial_status(OrderStatus.PENDING, "0.3", "0") == OrderStatus.PARTIAL


def test_terminal_statuses_pass_through_unchanged():
    # A terminal status is authoritative — never reinterpret it.
    for terminal in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
        assert resolve_partial_status(terminal, "0.5", "1.0") == terminal


def test_decimal_inputs_supported():
    assert resolve_partial_status(
        OrderStatus.PENDING, Decimal("0.5"), Decimal("1.0")
    ) == OrderStatus.PARTIAL
