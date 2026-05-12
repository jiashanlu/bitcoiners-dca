"""
SecretStore tests — encrypt/decrypt round-trip, redaction, key rotation.
"""
from __future__ import annotations

import os

import pytest

from bitcoiners_dca.persistence.secrets import (
    ENV_VAR,
    SecretStore,
    SecretStoreError,
    _redact,
    credentials_for,
    required_fields,
)


@pytest.fixture
def store(tmp_path):
    key = SecretStore.generate_key()
    s = SecretStore(tmp_path / "secrets.db", fernet_key=key)
    yield s
    s.close()


# === Round-trip ===

def test_set_get_roundtrip(store):
    store.set("okx.api_secret", "super-secret-token-1234567890")
    assert store.get("okx.api_secret") == "super-secret-token-1234567890"


def test_missing_returns_none(store):
    assert store.get("nonexistent.key") is None


def test_overwrite_replaces_value(store):
    store.set("k", "first")
    store.set("k", "second")
    assert store.get("k") == "second"


def test_delete_removes_secret(store):
    store.set("k", "v")
    assert store.delete("k") is True
    assert store.get("k") is None
    assert store.delete("k") is False  # already gone


def test_empty_key_rejected(store):
    with pytest.raises(SecretStoreError):
        store.set("", "value")


# === Listing + redaction ===

def test_list_returns_redacted_only(store):
    store.set("okx.api_secret", "very-long-secret-1234567890abcdef")
    store.set("bitoasis.token", "uuid-token-string-12345")
    entries = store.list()
    keys = {e.key for e in entries}
    assert keys == {"okx.api_secret", "bitoasis.token"}
    # None of the entries should expose plaintext
    for e in entries:
        assert "…" in e.redacted or e.redacted.startswith("•")


def test_redact_helper_never_leaks_any_chars():
    # `_redact` was leaking 6 of 9 chars of every credential. Now always
    # 8 bullets regardless of length. The only special-case is empty input.
    assert _redact("") == "(empty)"
    for plain in ["ab", "12345678", "very-long-secret", "A8x422xz@"]:
        out = _redact(plain)
        assert out == "••••••••", f"redaction leaked for {plain!r}: {out!r}"
        for ch in plain:
            assert ch not in out, f"char {ch!r} leaked through redaction"


# === Encryption strength ===

def test_wrong_key_cant_decrypt(tmp_path):
    k1 = SecretStore.generate_key()
    s1 = SecretStore(tmp_path / "db.db", fernet_key=k1)
    s1.set("k", "plaintext")
    s1.close()

    # Re-open with a different key
    k2 = SecretStore.generate_key()
    s2 = SecretStore(tmp_path / "db.db", fernet_key=k2)
    with pytest.raises(SecretStoreError, match="Decrypt failed"):
        s2.get("k")
    s2.close()


def test_invalid_key_raises_at_construction(tmp_path):
    with pytest.raises(SecretStoreError, match="Invalid"):
        SecretStore(tmp_path / "db.db", fernet_key="not-a-fernet-key")


def test_missing_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(SecretStoreError, match="Missing"):
        SecretStore(tmp_path / "db.db")


# === Key rotation ===

def test_rotate_key_reencrypts_all(tmp_path):
    k1 = SecretStore.generate_key()
    s = SecretStore(tmp_path / "db.db", fernet_key=k1)
    s.set("a", "alpha")
    s.set("b", "beta")

    k2 = SecretStore.generate_key()
    n = s.rotate_key(k2)
    assert n == 2

    # After rotation, the store keeps working with the new key
    assert s.get("a") == "alpha"
    assert s.get("b") == "beta"

    # And a fresh store with the OLD key fails
    s.close()
    s_old = SecretStore(tmp_path / "db.db", fernet_key=k1)
    with pytest.raises(SecretStoreError):
        s_old.get("a")
    s_old.close()


# === Adapter-credential helpers ===

def test_credentials_for_returns_namespaced_fields(store):
    store.set("okx.api_key", "key123")
    store.set("okx.api_secret", "secret456")
    store.set("okx.passphrase", "pass789")
    creds = credentials_for(store, "okx")
    assert creds == {
        "api_key": "key123",
        "api_secret": "secret456",
        "passphrase": "pass789",
    }


def test_credentials_for_omits_missing_fields(store):
    store.set("bitoasis.token", "abc")
    creds = credentials_for(store, "bitoasis")
    assert creds == {"token": "abc"}


def test_required_fields_per_exchange():
    assert required_fields("okx") == ["api_key", "api_secret", "passphrase"]
    assert required_fields("binance") == ["api_key", "api_secret"]
    assert required_fields("bitoasis") == ["token"]
    assert required_fields("nonexistent") == []
