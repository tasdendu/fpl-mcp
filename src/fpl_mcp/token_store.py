"""Persistent storage for the rotating FPL (PingOne OIDC) refresh token.

PingOne issues a new refresh token on every exchange and invalidates the old
one, so the latest token must survive restarts. The database is the source of
truth, with two ways to put a fresh token in:

* edit FPL_REFRESH_TOKEN in .env: a token the store has not seen as a seed
  before is adopted automatically on the next refresh;
* run ``fpl-mcp-set-token`` and paste the browser's oidc.user JSON.

Both take effect without restarting the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class StoredToken:
    refresh_token: str
    seed_fingerprint: str | None
    updated_at: int


class TokenStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS fpl_token (
                       id INTEGER PRIMARY KEY CHECK (id = 1),
                       refresh_token TEXT NOT NULL,
                       updated_at INTEGER NOT NULL)"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(fpl_token)")}
            if "seed_fingerprint" not in columns:
                conn.execute("ALTER TABLE fpl_token ADD COLUMN seed_fingerprint TEXT")
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def load(self) -> StoredToken | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT refresh_token, seed_fingerprint, updated_at FROM fpl_token WHERE id = 1"
            ).fetchone()
        return StoredToken(*row) if row else None

    def save(self, refresh_token: str, *, seed_fingerprint: str | None = None) -> None:
        """Store a token. Pass seed_fingerprint only when the token came from .env."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO fpl_token (id, refresh_token, updated_at, seed_fingerprint)
                   VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       refresh_token = excluded.refresh_token,
                       updated_at = excluded.updated_at,
                       seed_fingerprint = COALESCE(excluded.seed_fingerprint,
                                                   fpl_token.seed_fingerprint)""",
                (refresh_token, int(time.time()), seed_fingerprint),
            )

    def resolve(self, env_token: str | None) -> str | None:
        """Return the token to use, adopting a new .env token if one was set."""
        stored = self.load()
        if env_token:
            fingerprint = token_fingerprint(env_token)
            if stored is not None and stored.seed_fingerprint is None:
                # Row from before seed tracking: keep its (newer) token, remember the seed.
                self.save(stored.refresh_token, seed_fingerprint=fingerprint)
                return stored.refresh_token
            if stored is None or stored.seed_fingerprint != fingerprint:
                self.save(env_token, seed_fingerprint=fingerprint)
                return env_token
        return stored.refresh_token if stored else None


def parse_token_input(raw: str) -> str:
    """Accept a bare refresh token or the browser's oidc.user JSON value."""
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and data.get("refresh_token"):
        return str(data["refresh_token"])
    raise ValueError("JSON input has no refresh_token field")


def set_token_main() -> None:
    """CLI: store a fresh token read from stdin; the running server picks it up.

    docker compose exec -T fpl-mcp fpl-mcp-set-token   (paste, then Ctrl-D)
    """
    from .config import get_settings

    settings = get_settings()
    if not settings.fpl_token_db:
        sys.exit("FPL_TOKEN_DB is not set; nothing to write to")
    try:
        token = parse_token_input(sys.stdin.read())
    except ValueError as exc:
        sys.exit(str(exc))
    if not token:
        sys.exit("No token received on stdin")
    TokenStore(settings.fpl_token_db).save(token)
    print("FPL refresh token stored; the server will use it on its next refresh.")
