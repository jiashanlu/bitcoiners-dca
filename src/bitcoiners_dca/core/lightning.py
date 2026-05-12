"""
Lightning Network helpers — detection and normalization for invoice strings.

We accept BOLT11 invoices and on-chain addresses interchangeably in the
withdrawal flow; the caller picks the right network based on the prefix.
"""
from __future__ import annotations

import re
from enum import Enum

# BOLT11 invoice prefixes (BIP-173 bech32 HRPs)
_BOLT11_PREFIXES = ("lnbc", "lntb", "lnbcrt", "lnbs", "lnsb")

# LNURL bech32 prefix
_LNURL_PREFIX = "lnurl"

# Lightning Address — looks like email
_LN_ADDRESS_RE = re.compile(
    r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$"
)

# On-chain BTC address prefixes (P2PKH, P2SH, bech32 v0/v1)
_ONCHAIN_PREFIXES = ("1", "3", "bc1", "tb1", "bcrt1")


class WithdrawalNetwork(str, Enum):
    BITCOIN = "bitcoin"
    LIGHTNING = "lightning"
    LNURL = "lnurl"
    LIGHTNING_ADDRESS = "lightning_address"
    UNKNOWN = "unknown"


def detect_network(destination: str) -> WithdrawalNetwork:
    """Identify the withdrawal target type from its string form.

    Cases:
        bc1qxxx...           → BITCOIN
        lnbc1pvjluez...      → LIGHTNING (BOLT11 invoice)
        lnurl1dp68gurn...    → LNURL
        user@walletofsatoshi → LIGHTNING_ADDRESS
    """
    if not destination:
        return WithdrawalNetwork.UNKNOWN

    s = destination.strip().lower()

    if any(s.startswith(p) for p in _BOLT11_PREFIXES):
        return WithdrawalNetwork.LIGHTNING

    if s.startswith(_LNURL_PREFIX):
        return WithdrawalNetwork.LNURL

    if _LN_ADDRESS_RE.match(destination.strip()):
        return WithdrawalNetwork.LIGHTNING_ADDRESS

    if any(s.startswith(p) for p in _ONCHAIN_PREFIXES):
        return WithdrawalNetwork.BITCOIN

    return WithdrawalNetwork.UNKNOWN


def is_lightning(destination: str) -> bool:
    """True for any Lightning-flavored destination (invoice, LNURL, LN address)."""
    return detect_network(destination) in (
        WithdrawalNetwork.LIGHTNING,
        WithdrawalNetwork.LNURL,
        WithdrawalNetwork.LIGHTNING_ADDRESS,
    )
