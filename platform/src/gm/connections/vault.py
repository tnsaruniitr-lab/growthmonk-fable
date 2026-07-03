"""Sealed-box credential vault (Phase C wave-1 contract).

Credentials are encrypted with a libsodium SealedBox: anyone holding the PUBLIC
key can seal, only the holder of the PRIVATE key can open. Fetcher/API processes
run with `GM_VAULT_PUBLIC_KEY` only and can therefore store connections but never
read them back; only publisher/ingest workers mount `GM_VAULT_PRIVATE_KEY`.

`open_sealed` raises `VaultLocked` when the private key is absent — callers treat
that as "not my job", not as data corruption. v1 has a single key version; the
`key_version` parameter (and column) is reserved for future rotation.
"""

from __future__ import annotations

import base64
import json
import os

import psycopg
from nacl.public import PrivateKey, PublicKey, SealedBox
from psycopg.types.json import Jsonb

PUBLIC_KEY_ENV = "GM_VAULT_PUBLIC_KEY"
PRIVATE_KEY_ENV = "GM_VAULT_PRIVATE_KEY"


class VaultLocked(Exception):
    """The key needed for this operation is not present in the environment."""


def generate_keypair() -> tuple[str, str]:
    """Return (public_b64, private_b64). Operator runs once and stores both securely."""
    private = PrivateKey.generate()
    public_b64 = base64.b64encode(bytes(private.public_key)).decode("ascii")
    private_b64 = base64.b64encode(bytes(private)).decode("ascii")
    return public_b64, private_b64


def _key_bytes(env_name: str) -> bytes:
    raw = os.environ.get(env_name)
    if not raw:
        raise VaultLocked(f"{env_name} is not set")
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise VaultLocked(f"{env_name} is not valid base64") from exc


def seal(payload: dict) -> bytes:
    """Encrypt `payload` to the vault public key. Works without the private key."""
    box = SealedBox(PublicKey(_key_bytes(PUBLIC_KEY_ENV)))
    plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return bytes(box.encrypt(plaintext))


def open_sealed(blob: bytes, key_version: int = 1) -> dict:
    """Decrypt a sealed blob. Raises VaultLocked when GM_VAULT_PRIVATE_KEY is unset."""
    if key_version != 1:
        raise ValueError(f"unsupported key_version {key_version} (v1 is single-version)")
    box = SealedBox(PrivateKey(_key_bytes(PRIVATE_KEY_ENV)))
    plaintext = box.decrypt(bytes(blob))
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"sealed payload is not a JSON object: {type(data).__name__}")
    return data


def store_connection(
    conn: psycopg.Connection,
    *,
    org_id,
    site_id,
    kind: str,
    credentials: dict,
    meta: dict,
) -> str:
    """Seal `credentials` and upsert the (site_id, kind) connection row. Returns its id."""
    blob = seal(credentials)
    row = conn.execute(
        "insert into connections"
        " (org_id, site_id, kind, encrypted_credentials, key_version, status, meta)"
        " values (%s, %s, %s, %s, 1, 'ok', %s)"
        " on conflict (site_id, kind) do update set"
        "  encrypted_credentials = excluded.encrypted_credentials,"
        "  key_version = excluded.key_version,"
        "  status = 'ok',"
        "  meta = excluded.meta,"
        "  last_error = null"
        " returning id",
        (org_id, site_id, kind, blob, Jsonb(meta)),
    ).fetchone()
    return str(row["id"])


def load_connection(conn: psycopg.Connection, site_id, kind: str) -> dict:
    """Return the connection row with a decrypted `credentials` key (or raise VaultLocked).

    Reference-only rows (NULL encrypted_credentials) yield credentials=None.
    Raises LookupError when no such connection exists.
    """
    row = conn.execute(
        "select * from connections where site_id = %s and kind = %s",
        (site_id, kind),
    ).fetchone()
    if row is None:
        raise LookupError(f"no {kind!r} connection for site {site_id}")
    out = dict(row)
    blob = out.pop("encrypted_credentials")
    out["credentials"] = None if blob is None else open_sealed(blob, out["key_version"])
    return out


def mark_connection(
    conn: psycopg.Connection,
    connection_id,
    *,
    ok: bool,
    error: str | None = None,
) -> None:
    """Record the outcome of using a connection: ok -> status='ok', else 'broken'."""
    if ok:
        conn.execute(
            "update connections set status = 'ok', last_ok_at = now(), last_error = null"
            " where id = %s",
            (connection_id,),
        )
    else:
        conn.execute(
            "update connections set status = 'broken', last_error = %s where id = %s",
            (error, connection_id),
        )
