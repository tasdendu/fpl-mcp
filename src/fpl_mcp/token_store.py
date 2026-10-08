"""Persistent storage for the rotating FPL (PingOne OIDC) refresh token.

PingOne issues a new refresh token on every exchange and invalidates the old
one. Keeping the rotated token only in memory means a restart falls back to
the stale FPL_REFRESH_TOKEN in .env and every refresh fails. This store keeps
the latest token in SQLite on a mounted volume so it survives restarts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing, suppress
from pathlib import Path


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
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def load(self) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT refresh_token FROM fpl_token WHERE id = 1").fetchone()
        return row[0] if row else None

    def save(self, refresh_token: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO fpl_token (id, refresh_token, updated_at) VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       refresh_token = excluded.refresh_token,
                       updated_at = excluded.updated_at""",
                (refresh_token, int(time.time())),
            )


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
    """CLI: store a fresh token read from stdin.

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
    print("FPL refresh token stored. Restart the service to drop any cached access token.")
