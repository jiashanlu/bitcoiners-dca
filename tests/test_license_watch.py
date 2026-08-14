"""License expiry watch — the anti-silent-downgrade warning logic.

Born from the 2026-08-12 incident: a lapsed Pro license silently
downgraded a tenant to FREE and the bot risk-paused with no warning.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bitcoiners_dca.core.license import (
    License,
    LicenseTier,
    generate_keypair,
    sign_license,
)
from bitcoiners_dca.core.license_watch import (
    customer_message,
    pending_warning,
    record_warning,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def keypair():
    return generate_keypair()  # (private_pem, public_hex)


def token_expiring(keypair, days_from_now: float, tier=LicenseTier.PRO) -> str:
    private_pem, _ = keypair
    lic = License(
        tier=tier,
        customer_id="cust@example.com",
        issued_at=NOW - timedelta(days=90),
        expires_at=NOW + timedelta(days=days_from_now),
    )
    return sign_license(lic, private_pem)


def check(keypair, days_from_now, warned=None):
    _, public_hex = keypair
    return pending_warning(
        token_expiring(keypair, days_from_now), warned,
        now=NOW, public_key_hex=public_hex,
    )


class TestThresholds:
    def test_far_from_expiry_no_warning(self, keypair):
        assert check(keypair, 60) is None

    def test_inside_14d_warns_14(self, keypair):
        w = check(keypair, 10)
        assert w and w.threshold_days == 14

    def test_inside_3d_warns_3_not_14(self, keypair):
        w = check(keypair, 2)
        assert w and w.threshold_days == 3

    def test_expired_warns_0(self, keypair):
        w = check(keypair, -1)
        assert w and w.threshold_days == 0 and w.days_left < 0

    def test_only_most_severe_fires(self, keypair):
        # At T-2d with nothing warned yet, we get ONE message (the 3d one),
        # not a backlog of 14d + 3d.
        w = check(keypair, 2)
        assert w.threshold_days == 3


class TestDedup:
    def test_already_warned_threshold_is_silent(self, keypair):
        w = check(keypair, 10)
        warned = record_warning(None, w)
        assert check(keypair, 10, warned=warned) is None

    def test_next_threshold_still_fires_after_earlier_one(self, keypair):
        warned = record_warning(None, check(keypair, 10))  # 14d fired
        w = check(keypair, 2, warned=warned)
        assert w and w.threshold_days == 3

    def test_renewed_token_rearms(self, keypair):
        # Warn on the old expiry, then renew (new expires_at): the meta
        # entries no longer match, so the new key's thresholds are live.
        warned = record_warning(None, check(keypair, 2))
        assert check(keypair, 300, warned=warned) is None  # healthy again
        w = check(keypair, 10, warned=warned)  # next lapse approaches
        assert w and w.threshold_days == 14

    def test_record_warning_bounded_and_deduped(self, keypair):
        w = check(keypair, 10)
        v = record_warning("a:0,b:3", w)
        assert v.count(w.meta_entry) == 1
        v = record_warning(v, w)
        assert v.count(w.meta_entry) == 1


class TestNonWarnableTokens:
    def test_no_token_none(self):
        assert pending_warning(None, None, now=NOW) is None

    def test_garbage_token_none(self):
        assert pending_warning("not-a-token", None, now=NOW) is None

    def test_perpetual_license_none(self, keypair):
        private_pem, public_hex = keypair
        lic = License(
            tier=LicenseTier.PRO, customer_id="c@x.com",
            issued_at=NOW, expires_at=None,
        )
        token = sign_license(lic, private_pem)
        assert pending_warning(token, None, now=NOW,
                               public_key_hex=public_hex) is None


class TestMessages:
    def test_expiring_message_mentions_days_and_renewal(self, keypair):
        msg = customer_message(check(keypair, 10))
        assert "10 days" in msg and "app.bitcoiners.ae" in msg

    def test_expired_message_mentions_downgrade(self, keypair):
        msg = customer_message(check(keypair, -1))
        assert "EXPIRED" in msg and "Free tier" in msg
