"""
Tests for Lightning Network detection helpers + OKX chain resolution.
"""
from __future__ import annotations

import pytest

from bitcoiners_dca.core.lightning import (
    WithdrawalNetwork,
    detect_network,
    is_lightning,
)
from bitcoiners_dca.exchanges.base import WithdrawalDeniedError
from bitcoiners_dca.exchanges.okx import (
    OKX_CHAIN_BITCOIN,
    OKX_CHAIN_LIGHTNING,
    OKXExchange,
)


# === Detection ===

@pytest.mark.parametrize("addr,expected", [
    ("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", WithdrawalNetwork.BITCOIN),
    ("bc1pmzfrwwndsqmk5yh69yjr5lfgfg4ev8c0tsc06e", WithdrawalNetwork.BITCOIN),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", WithdrawalNetwork.BITCOIN),
    ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", WithdrawalNetwork.BITCOIN),
    ("LNBC1PVJLUEZPP5QQQSYQCYQ5RQWZQFQQQSY", WithdrawalNetwork.LIGHTNING),
    ("lnbc1p3xnmkdpp5cust0mp4ymen", WithdrawalNetwork.LIGHTNING),
    ("lntb1pdummy", WithdrawalNetwork.LIGHTNING),
    ("lnurl1dp68gurn8ghj7etcv9khqmr99e3k7mf0", WithdrawalNetwork.LNURL),
    ("ben@walletofsatoshi.com", WithdrawalNetwork.LIGHTNING_ADDRESS),
    ("user.name@strike.me", WithdrawalNetwork.LIGHTNING_ADDRESS),
    ("", WithdrawalNetwork.UNKNOWN),
    ("not-a-real-address", WithdrawalNetwork.UNKNOWN),
    ("0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb1", WithdrawalNetwork.UNKNOWN),
])
def test_detect_network(addr, expected):
    assert detect_network(addr) == expected


@pytest.mark.parametrize("addr,expected", [
    ("lnbc1pjexample", True),
    ("lnurl1dp68", True),
    ("user@wallet.com", True),
    ("bc1qabc", False),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", False),
    ("", False),
])
def test_is_lightning(addr, expected):
    assert is_lightning(addr) == expected


# === OKX chain resolution ===

class TestOkxResolveChain:
    def test_onchain_default(self):
        chain, net = OKXExchange._resolve_chain(
            "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", "bitcoin"
        )
        assert chain == OKX_CHAIN_BITCOIN
        assert net == "bitcoin"

    def test_lightning_explicit(self):
        chain, net = OKXExchange._resolve_chain("lnbc1pvjluez", "lightning")
        assert chain == OKX_CHAIN_LIGHTNING
        assert net == "lightning"

    def test_lightning_autodetected_from_invoice(self):
        """Pass network='bitcoin' but address is a BOLT11 invoice → switches to lightning."""
        chain, net = OKXExchange._resolve_chain("lnbc1pvjluez", "")
        assert chain == OKX_CHAIN_LIGHTNING
        assert net == "lightning"

    def test_rejects_lnurl(self):
        with pytest.raises(WithdrawalDeniedError, match="BOLT11"):
            OKXExchange._resolve_chain("lnurl1dp68", "lightning")

    def test_rejects_lightning_address(self):
        with pytest.raises(WithdrawalDeniedError, match="BOLT11"):
            OKXExchange._resolve_chain("ben@walletofsatoshi.com", "lightning")

    def test_rejects_mismatched_address_for_onchain(self):
        with pytest.raises(WithdrawalDeniedError, match="not on-chain"):
            OKXExchange._resolve_chain("lnbc1pvjluez", "bitcoin")

    def test_rejects_unsupported_network(self):
        with pytest.raises(WithdrawalDeniedError, match="Unsupported"):
            OKXExchange._resolve_chain("bc1qabc", "ethereum")
