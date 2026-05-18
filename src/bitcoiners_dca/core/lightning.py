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


async def resolve_to_invoice(destination: str, amount_sat: int) -> str:
    """Turn any Lightning-flavored destination into a BOLT11 invoice.

    OKX (and most exchange APIs) only accept raw BOLT11 invoices. When
    the user supplies a Lightning Address (`you@host`), we run the
    LNURL-pay flow against the host to request an invoice for the
    requested amount.

    Returns the BOLT11 string. Raises ValueError with an operator-
    readable message on any resolution failure.

    Supported:
      - BOLT11 invoice (lnbc…)         → passed through; the invoice's
        embedded amount governs (caller must match `amount_sat`).
      - Lightning Address (user@host)  → LUD-16 lnurlp resolution.

    Not yet supported:
      - LNURL (bech32 lnurl1…)         → would require bech32 decode.
        Most users encounter LN addresses, not raw LNURLs; deferred.
    """
    if amount_sat <= 0:
        raise ValueError("amount_sat must be > 0")

    net = detect_network(destination)

    if net == WithdrawalNetwork.LIGHTNING:
        # Already a BOLT11 invoice. We trust the caller has matched the
        # amount with what they typed in the form (the invoice carries
        # its own amount; if they mismatch, OKX will reject).
        return destination.strip()

    if net == WithdrawalNetwork.LIGHTNING_ADDRESS:
        return await _resolve_lightning_address(destination.strip(), amount_sat)

    if net == WithdrawalNetwork.LNURL:
        raise ValueError(
            "Raw LNURL bech32 strings aren't supported yet — "
            "paste the Lightning Address (e.g. you@getalby.com) or a BOLT11 invoice instead."
        )

    raise ValueError(
        f"Destination '{destination}' isn't a recognised Lightning target. "
        "Use a BOLT11 invoice (lnbc…) or a Lightning Address (you@host)."
    )


async def _resolve_lightning_address(address: str, amount_sat: int) -> str:
    """LUD-16 (LN address) → BOLT11 invoice.

    Flow:
      1. GET https://<host>/.well-known/lnurlp/<user> → LNURL-pay metadata
      2. Validate amount fits [minSendable, maxSendable] (millisatoshis)
      3. GET <callback>?amount=<msat>                → invoice JSON
      4. Return invoice.pr (the BOLT11 string)

    Errors bubble up as ValueError with operator-readable messages so
    the dashboard can flash them straight to the user.
    """
    import httpx

    user, _, host = address.partition("@")
    if not user or not host:
        raise ValueError(f"Lightning Address '{address}' must look like user@host")

    msat = amount_sat * 1000
    metadata_url = f"https://{host}/.well-known/lnurlp/{user}"

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            r = await client.get(metadata_url)
        except httpx.HTTPError as e:
            raise ValueError(f"Couldn't reach {host}: {e}") from e
        if r.status_code != 200:
            raise ValueError(
                f"LN address lookup failed: {r.status_code} from {metadata_url}"
            )
        try:
            meta = r.json()
        except Exception as e:
            raise ValueError(f"LN address metadata wasn't JSON: {e}") from e

        if meta.get("tag") != "payRequest":
            raise ValueError(
                f"LN address {address} doesn't advertise a payRequest tag — got "
                f"{meta.get('tag')!r}"
            )

        min_msat = int(meta.get("minSendable", 0))
        max_msat = int(meta.get("maxSendable", 0))
        if min_msat and msat < min_msat:
            raise ValueError(
                f"Amount ({amount_sat} sat) is below the address minimum "
                f"({min_msat // 1000} sat)"
            )
        if max_msat and msat > max_msat:
            raise ValueError(
                f"Amount ({amount_sat} sat) is above the address maximum "
                f"({max_msat // 1000} sat)"
            )

        callback = meta.get("callback")
        if not callback:
            raise ValueError(f"LN address {address} returned no callback URL")

        try:
            r2 = await client.get(callback, params={"amount": msat})
        except httpx.HTTPError as e:
            raise ValueError(f"LN address callback failed: {e}") from e
        if r2.status_code != 200:
            raise ValueError(
                f"LN address callback returned {r2.status_code}"
            )
        try:
            cb = r2.json()
        except Exception as e:
            raise ValueError(f"LN address callback wasn't JSON: {e}") from e

        # Some providers return errors as {"status":"ERROR","reason":"..."}.
        if cb.get("status") == "ERROR":
            raise ValueError(
                f"LN address callback rejected the request: {cb.get('reason', 'unknown error')}"
            )

        invoice = cb.get("pr")
        if not invoice or not invoice.lower().startswith(_BOLT11_PREFIXES):
            raise ValueError(
                f"LN address callback didn't return a BOLT11 invoice. Got: {cb!r}"
            )
        return invoice
