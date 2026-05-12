"""
License + tier management — gates premium features in the bot.

Three tiers:

  FREE      Self-host. Single exchange. Basic DCA + tax CSV + on-chain
            auto-withdraw + risk circuit breakers + local dashboard.
            Genuinely useful as standalone software.

  PRO       Hosted (or self-host with a valid key). Multi-exchange smart
            routing, multi-hop, maker mode, Lightning auto-withdraw,
            cross-exchange arb alerts, funding-rate monitor, advanced
            strategy overlays (volatility-weighted, time-of-day, drawdown).

  BUSINESS  Pro + basis-trade execution, LN Markets covered-call yield,
            multi-asset DCA, stablecoin yield, tax-loss harvesting,
            family-office multi-strategy mode.

A license is a base64-encoded JSON payload signed with Ed25519. The bot
ships with a hardcoded public key; only the holder of the matching
private key (Ben / bitcoiners.ae) can issue valid licenses. Verification
is OFFLINE — no phone-home required.

Free tier needs no key. Pro/Business need a key signed by the bot's
publisher. The license check is intentionally NOT obfuscated: someone
determined to fork-and-strip can do so. The license is a *value
proposition*, not DRM. The aim is to make the hosted tier easy enough
to pay for that most users prefer it.

See `docs/TIERS.md` for the feature matrix and `scripts/generate_license.py`
for the issuance tool.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)


# === Tier definitions ===

class LicenseTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"


# Feature identifiers — checked by `is_feature_enabled()`. Keeping this as a
# central registry rather than scattered string literals makes typos
# impossible and gives us a single place to audit gating.
class Feature(str, Enum):
    # Multi-exchange ops
    MULTI_EXCHANGE = "multi_exchange"          # enable ≥2 exchanges at once
    MULTI_HOP_ROUTING = "multi_hop_routing"    # AED→USDT→BTC synthetic paths
    CROSS_EXCHANGE_ALERTS = "cross_exchange_alerts"

    # Execution
    MAKER_MODE = "maker_mode"                  # limit orders instead of market

    # Strategy overlays
    DIP_OVERLAY = "dip_overlay"
    VOLATILITY_WEIGHTED = "volatility_weighted_dca"
    TIME_OF_DAY = "time_of_day_dca"
    DRAWDOWN_SIZING = "drawdown_aware_sizing"

    # Withdrawal
    LIGHTNING_WITHDRAW = "lightning_withdraw"

    # Monitors
    FUNDING_MONITOR = "funding_monitor"

    # Business-tier features
    BASIS_TRADE = "basis_trade_execution"
    LN_MARKETS_YIELD = "ln_markets_covered_calls"
    MULTI_ASSET_DCA = "multi_asset_dca"
    STABLECOIN_YIELD = "stablecoin_yield"
    TAX_LOSS_HARVEST = "tax_loss_harvesting"
    FAMILY_OFFICE = "family_office_multi_strategy"


# Tier → set of features.
#
# v0.7 pivot: ALL features are available to ALL tiers. The software is
# fully open-source; we sell hosting + services, not feature unlocks.
# (See docs/PRICING_PHILOSOPHY.md for the rationale.)
#
# The tier value is still meaningful to the rest of the bot — it
# distinguishes "this customer is self-hosting" from "this customer is on
# our hosted infra" for things like the dashboard's tier badge, telemetry
# of which features hosted customers actually use, and where premium
# Business-tier services like onboarding calls get scheduled. But the
# feature set is identical across tiers.
_ALL_FEATURES: set[Feature] = set(Feature)

_TIER_FEATURES: dict[LicenseTier, set[Feature]] = {
    LicenseTier.FREE: _ALL_FEATURES,
    LicenseTier.PRO: _ALL_FEATURES,
    LicenseTier.BUSINESS: _ALL_FEATURES,
}

# Retained for the type-checker so the rest of the module still resolves
# the same names; previously these were used to build up tiers.
_LEGACY_PRO_FEATURES = {
        Feature.MULTI_EXCHANGE,
        Feature.MULTI_HOP_ROUTING,
        Feature.CROSS_EXCHANGE_ALERTS,
        Feature.MAKER_MODE,
        Feature.DIP_OVERLAY,
        Feature.VOLATILITY_WEIGHTED,
        Feature.TIME_OF_DAY,
        Feature.DRAWDOWN_SIZING,
        Feature.LIGHTNING_WITHDRAW,
        Feature.FUNDING_MONITOR,
}
_LEGACY_BUSINESS_FEATURES = {
        Feature.BASIS_TRADE,
        Feature.LN_MARKETS_YIELD,
        Feature.MULTI_ASSET_DCA,
        Feature.STABLECOIN_YIELD,
        Feature.TAX_LOSS_HARVEST,
        Feature.FAMILY_OFFICE,
}


# === Signing key — public-key embedded in the bot ===
#
# This is the public half of a generated Ed25519 keypair. The private half
# lives in Ben's secrets, not in the repo. To rotate: generate a new
# keypair, replace `LICENSE_PUBLIC_KEY_HEX` here, re-issue customer keys.
# (Old keys signed with the previous key will then fail verification.)
#
# Bootstrap value below is generated by `scripts/generate_license.py keygen`.
# For development we ship a key — Ben can rotate when going to production.
LICENSE_PUBLIC_KEY_HEX = (
    # Bootstrap public key generated 2026-05-12. Private key lives at
    # workspace/infra/dca_license_signing_key.pem (chmod 600, not in this repo).
    # Rotate by regenerating with scripts/generate_license.py keygen and
    # replacing this constant. All previously-issued license tokens become
    # invalid after rotation.
    "959a5f527d993fdf7f16c1c185bb6e829f905aebfe33e2ef8a4e63d409cd3a3e"
)


# === License model ===

@dataclass(frozen=True)
class License:
    tier: LicenseTier
    customer_id: str
    issued_at: datetime
    expires_at: Optional[datetime] = None
    notes: str = ""

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def to_payload(self) -> dict:
        return {
            "tier": self.tier.value,
            "customer_id": self.customer_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "notes": self.notes,
        }


class LicenseError(Exception):
    """Raised when a license string is malformed, expired, or has a bad signature."""


def parse_license_token(token: str, public_key_hex: str) -> License:
    """Decode + verify a license token.

    Token format: `base64(json_payload).base64(signature)`.

    Raises LicenseError on any failure (bad encoding, bad signature, expired).
    """
    if not token or "." not in token:
        raise LicenseError("Token is empty or missing signature separator")
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        signature_bytes = base64.urlsafe_b64decode(sig_b64 + "==")
    except Exception as e:
        raise LicenseError(f"Token base64 decode failed: {e}") from e

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(public_key_hex)
        )
    except Exception as e:
        raise LicenseError(f"Public key invalid: {e}") from e

    try:
        public_key.verify(signature_bytes, payload_bytes)
    except InvalidSignature as e:
        raise LicenseError("Signature verification failed — token tampered or "
                           "signed by a different key") from e

    try:
        data = json.loads(payload_bytes)
        return License(
            tier=LicenseTier(data["tier"]),
            customer_id=str(data["customer_id"]),
            issued_at=datetime.fromisoformat(data["issued_at"]),
            expires_at=(
                datetime.fromisoformat(data["expires_at"])
                if data.get("expires_at") else None
            ),
            notes=data.get("notes", ""),
        )
    except (KeyError, ValueError) as e:
        raise LicenseError(f"Payload parse failed: {e}") from e


# === Manager ===

class LicenseManager:
    """Single source of truth for "is this feature available right now?"

    Constructed once at boot from config. `is_feature_enabled()` is the
    only thing the rest of the codebase calls.
    """

    def __init__(
        self,
        tier: LicenseTier = LicenseTier.FREE,
        license_obj: Optional[License] = None,
    ):
        self.tier = tier
        self.license = license_obj
        # Pre-compute the feature set so checks are O(1).
        self._features = _TIER_FEATURES.get(tier, set())

    @classmethod
    def from_config(
        cls,
        tier_str: str,
        license_key: Optional[str],
        public_key_hex: str = LICENSE_PUBLIC_KEY_HEX,
    ) -> "LicenseManager":
        """Build a manager from config values.

        - Tier `free` always works without a key.
        - Tier `pro` / `business` REQUIRES a key signed for that tier.
        - An invalid/missing key downgrades silently to free + logs a warning.
        """
        try:
            requested_tier = LicenseTier(tier_str.lower())
        except ValueError:
            logger.warning(
                "Unknown license tier %r in config — falling back to FREE",
                tier_str,
            )
            return cls(LicenseTier.FREE)

        if requested_tier == LicenseTier.FREE:
            return cls(LicenseTier.FREE)

        if not license_key:
            logger.warning(
                "Config requested tier=%s but no license key provided — "
                "downgrading to FREE. Get a key at https://bitcoiners.ae/dca-bot",
                requested_tier.value,
            )
            return cls(LicenseTier.FREE)

        if public_key_hex == "BOOTSTRAP_PUBLIC_KEY_PLACEHOLDER":
            logger.warning(
                "License signing key not configured (publisher must replace "
                "the BOOTSTRAP placeholder before issuing keys). Downgrading "
                "to FREE."
            )
            return cls(LicenseTier.FREE)

        try:
            lic = parse_license_token(license_key, public_key_hex)
        except LicenseError as e:
            logger.warning("License key rejected: %s — downgrading to FREE", e)
            return cls(LicenseTier.FREE)

        if lic.is_expired:
            logger.warning(
                "License for %s expired on %s — downgrading to FREE",
                lic.customer_id, lic.expires_at,
            )
            return cls(LicenseTier.FREE, license_obj=lic)

        if lic.tier != requested_tier:
            logger.warning(
                "Config requested %s but license is %s — using license tier",
                requested_tier.value, lic.tier.value,
            )

        return cls(tier=lic.tier, license_obj=lic)

    def is_feature_enabled(self, feature: Feature) -> bool:
        return feature in self._features

    @property
    def enabled_features(self) -> list[Feature]:
        return sorted(self._features, key=lambda f: f.value)

    def describe(self) -> dict:
        """For the `license` CLI + dashboard surface."""
        out = {
            "tier": self.tier.value,
            "feature_count": len(self._features),
            "features": [f.value for f in self.enabled_features],
        }
        if self.license:
            out["customer_id"] = self.license.customer_id
            out["issued_at"] = self.license.issued_at.isoformat()
            out["expires_at"] = (
                self.license.expires_at.isoformat()
                if self.license.expires_at else "never"
            )
            out["notes"] = self.license.notes
        return out


# === Helpers for tooling ===

def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair. Returns (private_pem, public_hex).

    Private PEM is meant to live in `workspace/infra/license_signing_key.pem`
    with mode 0600. Public hex goes into `LICENSE_PUBLIC_KEY_HEX`.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_hex = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    return private_pem, public_hex


def sign_license(license_obj: License, private_pem: str) -> str:
    """Sign a License and return the `payload.signature` token."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = serialization.load_pem_private_key(
        private_pem.encode(), password=None
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise LicenseError("Expected Ed25519 private key")
    payload_bytes = json.dumps(
        license_obj.to_payload(), sort_keys=True, separators=(",", ":")
    ).encode()
    signature = private_key.sign(payload_bytes)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"
