"""
License framework tests — tier enforcement, signature verification, expiry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bitcoiners_dca.core.license import (
    Feature,
    License,
    LicenseError,
    LicenseManager,
    LicenseTier,
    generate_keypair,
    parse_license_token,
    sign_license,
)


# === Tier feature membership ===
# v0.7 pivot: every tier unlocks every feature. The license framework is
# retained for tier-identification (hosted vs self-host) but doesn't gate
# software capabilities. See core/license.py docstring + pricing page for
# the rationale.

def test_free_tier_has_every_feature():
    mgr = LicenseManager(LicenseTier.FREE)
    assert mgr.is_feature_enabled(Feature.MULTI_EXCHANGE)
    assert mgr.is_feature_enabled(Feature.MULTI_HOP_ROUTING)
    assert mgr.is_feature_enabled(Feature.MAKER_MODE)
    assert mgr.is_feature_enabled(Feature.FUNDING_MONITOR)
    assert mgr.is_feature_enabled(Feature.BASIS_TRADE)
    assert mgr.is_feature_enabled(Feature.LN_MARKETS_YIELD)


def test_pro_tier_has_every_feature():
    mgr = LicenseManager(LicenseTier.PRO)
    for f in Feature:
        assert mgr.is_feature_enabled(f), f"PRO tier missing {f}"


def test_business_tier_has_every_feature():
    mgr = LicenseManager(LicenseTier.BUSINESS)
    for f in Feature:
        assert mgr.is_feature_enabled(f), f"BUSINESS tier missing {f}"


# === Token sign + verify round-trip ===

@pytest.fixture
def keypair():
    private_pem, public_hex = generate_keypair()
    return private_pem, public_hex


def test_signed_token_round_trip(keypair):
    private_pem, public_hex = keypair
    lic = License(
        tier=LicenseTier.PRO,
        customer_id="alice@example.com",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        notes="round-trip test",
    )
    token = sign_license(lic, private_pem)
    decoded = parse_license_token(token, public_hex)
    assert decoded.tier == LicenseTier.PRO
    assert decoded.customer_id == "alice@example.com"
    assert decoded.notes == "round-trip test"


def test_signature_rejection_on_wrong_key(keypair):
    private_pem, public_hex = keypair
    lic = License(
        tier=LicenseTier.PRO, customer_id="x",
        issued_at=datetime.now(timezone.utc),
    )
    token = sign_license(lic, private_pem)

    # Generate a DIFFERENT keypair and use its public key
    _, other_public = generate_keypair()
    with pytest.raises(LicenseError, match="Signature"):
        parse_license_token(token, other_public)


def test_malformed_token_rejected(keypair):
    _, public_hex = keypair
    with pytest.raises(LicenseError):
        parse_license_token("not-a-token", public_hex)
    with pytest.raises(LicenseError):
        parse_license_token("", public_hex)
    with pytest.raises(LicenseError):
        parse_license_token("AAA.BBB", public_hex)


# === LicenseManager.from_config ===

def test_from_config_free_no_key():
    mgr = LicenseManager.from_config("free", None)
    assert mgr.tier == LicenseTier.FREE


def test_from_config_pro_without_key_downgrades_to_free():
    mgr = LicenseManager.from_config("pro", None)
    assert mgr.tier == LicenseTier.FREE


def test_from_config_pro_with_invalid_key_downgrades():
    mgr = LicenseManager.from_config(
        "pro", "totally-fake-token",
        public_key_hex="00" * 32,  # fake but valid-shaped public key
    )
    assert mgr.tier == LicenseTier.FREE


def test_from_config_pro_with_valid_key(keypair):
    private_pem, public_hex = keypair
    lic = License(
        tier=LicenseTier.PRO, customer_id="bob@example.com",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    token = sign_license(lic, private_pem)
    mgr = LicenseManager.from_config("pro", token, public_key_hex=public_hex)
    assert mgr.tier == LicenseTier.PRO
    assert mgr.is_feature_enabled(Feature.MULTI_HOP_ROUTING)


def test_expired_license_downgrades_to_free(keypair):
    private_pem, public_hex = keypair
    lic = License(
        tier=LicenseTier.PRO, customer_id="expired@example.com",
        issued_at=datetime.now(timezone.utc) - timedelta(days=400),
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    token = sign_license(lic, private_pem)
    mgr = LicenseManager.from_config("pro", token, public_key_hex=public_hex)
    assert mgr.tier == LicenseTier.FREE


def test_from_config_unknown_tier_falls_back_to_free():
    mgr = LicenseManager.from_config("enterprise", None)
    assert mgr.tier == LicenseTier.FREE


# === Describe surface ===

def test_describe_contains_customer_info_when_licensed(keypair):
    private_pem, public_hex = keypair
    lic = License(
        tier=LicenseTier.BUSINESS, customer_id="biz@example.com",
        issued_at=datetime.now(timezone.utc),
        notes="enterprise pilot",
    )
    token = sign_license(lic, private_pem)
    mgr = LicenseManager.from_config("business", token, public_key_hex=public_hex)
    info = mgr.describe()
    assert info["tier"] == "business"
    assert info["customer_id"] == "biz@example.com"
    assert info["notes"] == "enterprise pilot"
    assert "multi_asset_dca" in info["features"]
