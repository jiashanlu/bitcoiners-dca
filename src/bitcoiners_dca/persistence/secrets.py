"""
Encrypted secrets at rest — Fernet-symmetric storage in SQLite.

Customers paste API keys + tokens into the web dashboard. We can't store
them in the YAML config (visible on disk to anyone with file access) and
we don't want env vars per secret (won't survive a config-from-UI flow).

So: encrypted blobs in a dedicated `secrets` table, key from `DCA_SECRETS_KEY`
environment variable. Decrypted only at use-time inside the daemon (when
constructing exchange adapters). Dashboard reads back only the redacted form
(`abc…ef4`), never the plaintext.

Threat model:
  * Filesystem attacker with read access to data/dca.db → sees ciphertext, can't
    decrypt without DCA_SECRETS_KEY.
  * Filesystem attacker with read access to .env → has DCA_SECRETS_KEY but no
    ciphertext.
  * Both → game over. Goal: don't lose both at once.

For hosted-tenant deployment, DCA_SECRETS_KEY lives in the per-tenant .env
(chmod 600, mounted from a separate volume from data/). For self-host, the
user generates a key with `bitcoiners-dca secrets keygen` and keeps it safe.

Rotation: re-encrypt all secrets under a new key by calling
`SecretStore.rotate_key(old, new)`. Old ciphertexts become invalid.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENV_VAR = "DCA_SECRETS_KEY"


@dataclass(frozen=True)
class SecretEntry:
    key: str            # logical name, e.g. "okx.api_secret"
    redacted: str       # safe-to-display form
    updated_at: datetime


class SecretStoreError(Exception):
    pass


class SecretStore:
    """CRUD + Fernet encryption over a SQLite table.

    Schema:
        secrets(key TEXT PRIMARY KEY,
                ciphertext BLOB NOT NULL,
                updated_at TEXT NOT NULL)
    """

    def __init__(self, db_path: str | Path, fernet_key: Optional[str] = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        key = fernet_key or os.environ.get(ENV_VAR)
        if not key:
            raise SecretStoreError(
                f"Missing {ENV_VAR} env var. Generate one with "
                f"`bitcoiners-dca secrets keygen` and add it to .env."
            )
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as e:
            raise SecretStoreError(
                f"Invalid {ENV_VAR} — must be a 32-byte url-safe base64 string. "
                f"Generate with `bitcoiners-dca secrets keygen`."
            ) from e

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS secrets (
                   key        TEXT PRIMARY KEY,
                   ciphertext BLOB NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )

    # --- public API ---

    def set(self, key: str, value: str) -> None:
        """Encrypt + store. Overwrites any existing entry at this key."""
        if not key:
            raise SecretStoreError("Secret key cannot be empty")
        ciphertext = self._fernet.encrypt(value.encode())
        self._conn.execute(
            """INSERT INTO secrets (key, ciphertext, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 ciphertext = excluded.ciphertext,
                 updated_at = excluded.updated_at""",
            (key, ciphertext, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def get(self, key: str) -> Optional[str]:
        """Return plaintext value, or None if absent. Raises if key is corrupt."""
        cur = self._conn.execute(
            "SELECT ciphertext FROM secrets WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row["ciphertext"]).decode()
        except InvalidToken as e:
            raise SecretStoreError(
                f"Decrypt failed for {key!r} — wrong DCA_SECRETS_KEY?"
            ) from e

    def delete(self, key: str) -> bool:
        cur = self._conn.execute("DELETE FROM secrets WHERE key = ?", (key,))
        self._conn.commit()
        return cur.rowcount > 0

    def list(self) -> list[SecretEntry]:
        """All stored secrets in redacted form — safe to render in UI."""
        cur = self._conn.execute(
            "SELECT key, ciphertext, updated_at FROM secrets ORDER BY key"
        )
        out: list[SecretEntry] = []
        for row in cur.fetchall():
            try:
                plain = self._fernet.decrypt(row["ciphertext"]).decode()
                redacted = _redact(plain)
            except InvalidToken:
                redacted = "(decrypt-failed)"
            out.append(SecretEntry(
                key=row["key"], redacted=redacted,
                updated_at=datetime.fromisoformat(row["updated_at"]),
            ))
        return out

    def rotate_key(self, new_fernet_key: str) -> int:
        """Re-encrypt every secret under `new_fernet_key`. Returns count rotated.

        After this returns, callers should swap their `DCA_SECRETS_KEY` env to
        the new value. Old key no longer works.
        """
        try:
            new_fernet = Fernet(new_fernet_key.encode())
        except Exception as e:
            raise SecretStoreError("New Fernet key invalid") from e

        cur = self._conn.execute("SELECT key, ciphertext FROM secrets")
        rotated = 0
        for row in cur.fetchall():
            plain = self._fernet.decrypt(row["ciphertext"])
            new_ct = new_fernet.encrypt(plain)
            self._conn.execute(
                """UPDATE secrets SET ciphertext = ?, updated_at = ?
                   WHERE key = ?""",
                (new_ct, datetime.now(timezone.utc).isoformat(), row["key"]),
            )
            rotated += 1
        self._conn.commit()
        self._fernet = new_fernet
        return rotated

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def generate_key() -> str:
        """Mint a new Fernet key — 32-byte url-safe base64 string."""
        return Fernet.generate_key().decode()


def _redact(value: str) -> str:
    """All-bullets redaction. Showing any prefix/suffix of a credential
    leaks too much for short secrets — e.g. a 9-char OKX passphrase
    becomes `A8x…xz@`, revealing 67% of the secret to anyone who can read
    the rendered HTML. Now: always 8 bullets, no exception."""
    if not value:
        return "(empty)"
    return "••••••••"


# === Adapter-credential helpers ===
#
# Convention: per-exchange secrets live under namespaced keys so we don't
# leak names across exchanges.
#   okx.api_key, okx.api_secret, okx.passphrase
#   binance.api_key, binance.api_secret
#   bitoasis.token
#   telegram.bot_token

_EXCHANGE_KEYS: dict[str, list[str]] = {
    "okx": ["api_key", "api_secret", "passphrase"],
    "binance": ["api_key", "api_secret"],
    "bitoasis": ["token"],
}


def credentials_for(store: SecretStore, exchange: str) -> dict[str, str]:
    """Return a {field: value} mapping, omitting absent fields."""
    fields = _EXCHANGE_KEYS.get(exchange, [])
    out: dict[str, str] = {}
    for field in fields:
        val = store.get(f"{exchange}.{field}")
        if val:
            out[field] = val
    return out


def required_fields(exchange: str) -> list[str]:
    return _EXCHANGE_KEYS.get(exchange, [])
