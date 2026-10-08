"""Minimal OAuth 2.1 authorization server so Claude can connect to the pool as a custom connector.

Implements what Claude's connector client needs: dynamic client registration (RFC 7591), authorization code
with PKCE S256, and refresh tokens that rotate on use. The owner proves identity on the consent page by typing
the pool token. Codes and tokens are stored hashed in the pool's SQLite file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 86400
CODE_TTL_S = 600
MAX_CLIENTS = 200
MAX_FAILURES = 10
FAILURE_WINDOW_S = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    secret_hash TEXT,
    redirect_uris TEXT NOT NULL,
    name TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    challenge TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    client_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(error)
        self.error, self.description, self.status = error, description, status


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _is_loopback(parts: urllib.parse.SplitResult) -> bool:
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def redirect_allowed(uri: str) -> bool:
    """Only Claude's own callbacks may be registered: the hosted apps and Claude Code's loopback."""
    try:
        parts = urllib.parse.urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    if parts.fragment or parts.username or parts.query:
        return False
    if parts.scheme == "https" and parts.hostname in ("claude.ai", "claude.com"):
        return parts.path == "/api/mcp/auth_callback" and parts.port is None
    return _is_loopback(parts) and parts.path == "/callback"


def redirect_matches(registered: list[str], uri: str) -> bool:
    """Exact match, except that loopback redirects ignore the port (RFC 8252: Claude Code picks a free one)."""
    if uri in registered:
        return True
    try:
        want = urllib.parse.urlsplit(uri)
        if not _is_loopback(want):
            return False
        for r in registered:
            have = urllib.parse.urlsplit(r)
            if _is_loopback(have) and have.hostname == want.hostname and have.path == want.path:
                return True
    except ValueError:
        return False
    return False


class OAuth:
    def __init__(self, db_path: Path, owner_token: str):
        self.db_path = Path(db_path)
        self.owner_token = owner_token
        self._failures: list[float] = []
        self._lock = threading.Lock()
        conn = self._db()
        conn.executescript(SCHEMA)
        conn.close()

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- owner check on the consent page ------------------------------------

    def check_owner_token(self, presented: str) -> str:
        """Returns "ok", "bad" or "locked" (too many wrong attempts in the last 10 minutes)."""
        with self._lock:
            now = time.time()
            self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW_S]
            if len(self._failures) >= MAX_FAILURES:
                return "locked"
            if hmac.compare_digest(presented.encode(), self.owner_token.encode()):
                return "ok"
            self._failures.append(now)
            return "bad"

    # ---- clients -------------------------------------------------------------

    def register(self, meta: dict[str, Any]) -> dict[str, Any]:
        uris = meta.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not uris
            or len(uris) > 5
            or not all(isinstance(u, str) and redirect_allowed(u) for u in uris)
        ):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be Claude's callback URLs")
        method = meta.get("token_endpoint_auth_method") or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError("invalid_client_metadata", "unsupported token_endpoint_auth_method")
        name = str(meta.get("client_name") or "MCP client")[:80]
        client_id = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        now = time.time()
        conn = self._db()
        try:
            conn.execute(
                "INSERT INTO oauth_clients (client_id, secret_hash, redirect_uris, name, created_at) VALUES (?,?,?,?,?)",
                (client_id, _hash(secret) if secret else None, json.dumps(uris), name, now),
            )
            # Registration is open (that is how DCR works), so keep the table bounded.
            conn.execute(
                "DELETE FROM oauth_clients WHERE client_id IN (SELECT client_id FROM oauth_clients"
                " ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (MAX_CLIENTS,),
            )
        finally:
            conn.close()
        out: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(now),
            "client_name": name,
            "redirect_uris": uris,
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        if secret:
            out["client_secret"] = secret
            out["client_secret_expires_at"] = 0
        return out

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        conn = self._db()
        try:
            row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"client_id": row["client_id"], "name": row["name"], "secret_hash": row["secret_hash"],
                "redirect_uris": json.loads(row["redirect_uris"])}

    def _authenticate(self, client_id: str, client_secret: str | None) -> dict[str, Any]:
        client = self.get_client(client_id or "")
        if client is None:
            raise OAuthError("invalid_client", "unknown client", 401)
        if client["secret_hash"] and not (
            client_secret and hmac.compare_digest(_hash(client_secret), client["secret_hash"])
        ):
            raise OAuthError("invalid_client", "bad client credentials", 401)
        return client

    # ---- authorization code ----------------------------------------------------

    def create_code(self, client_id: str, redirect_uri: str, challenge: str) -> str:
        code = secrets.token_urlsafe(32)
        conn = self._db()
        try:
            conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (time.time(),))
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, challenge, expires_at) VALUES (?,?,?,?,?)",
                (_hash(code), client_id, redirect_uri, challenge, time.time() + CODE_TTL_S),
            )
        finally:
            conn.close()
        return code

    def exchange_code(
        self, client_id: str, client_secret: str | None, code: str, redirect_uri: str, verifier: str
    ) -> dict[str, Any]:
        self._authenticate(client_id, client_secret)
        conn = self._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (_hash(code or ""),)).fetchone()
            if row is not None:  # a code works once, even if the checks below fail
                conn.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (row["code_hash"],))
            conn.execute("COMMIT")
        finally:
            conn.close()
        if row is None or row["expires_at"] < time.time():
            raise OAuthError("invalid_grant", "code is invalid or expired")
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            raise OAuthError("invalid_grant", "code was issued to a different client or redirect_uri")
        if not (43 <= len(verifier or "") <= 128):
            raise OAuthError("invalid_grant", "bad code_verifier")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, row["challenge"]):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        return self._issue(client_id)

    # ---- tokens ------------------------------------------------------------------

    def _issue(self, client_id: str) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        conn = self._db()
        try:
            conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
            conn.execute("INSERT INTO oauth_tokens VALUES (?,?,?,?)", (_hash(access), "access", client_id, now + ACCESS_TTL_S))
            conn.execute("INSERT INTO oauth_tokens VALUES (?,?,?,?)", (_hash(refresh), "refresh", client_id, now + REFRESH_TTL_S))
        finally:
            conn.close()
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                "refresh_token": refresh, "scope": "mcp"}

    def refresh(self, client_id: str, client_secret: str | None, refresh_token: str) -> dict[str, Any]:
        self._authenticate(client_id, client_secret)
        conn = self._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'", (_hash(refresh_token or ""),)
            ).fetchone()
            if row is not None:  # rotation: the presented refresh token is spent
                conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (row["token_hash"],))
            conn.execute("COMMIT")
        finally:
            conn.close()
        if row is None or row["client_id"] != client_id or row["expires_at"] < time.time():
            raise OAuthError("invalid_grant", "refresh token is invalid or expired")
        return self._issue(client_id)

    def validate_access(self, token: str) -> bool:
        if not token:
            return False
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT 1 FROM oauth_tokens WHERE token_hash = ? AND kind = 'access' AND expires_at > ?",
                (_hash(token), time.time()),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
