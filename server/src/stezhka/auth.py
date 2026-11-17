"""Single-owner authentication: one password (scrypt) and server-side sessions.

Session tokens are random, sent as an HttpOnly SameSite=Strict cookie, and only their SHA-256 is
stored, so a leaked database does not leak live sessions. Device tokens for a future Android app
can be added next to this without changing the web login.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

from .db import get_setting, set_setting

SESSION_COOKIE = "stezhka_session"
SESSION_TTL_S = 30 * 24 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # ~16 MiB, ~50 ms on an old i3
MIN_PASSWORD_LEN = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${dk.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), dklen=len(dk) // 2, n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), dk)


def set_password(conn: sqlite3.Connection, password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    set_setting(conn, "password", hash_password(password))
    conn.execute("DELETE FROM sessions")  # changing the password logs out everywhere


def has_password(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "password") is not None


def check_password(conn: sqlite3.Connection, password: str) -> bool:
    stored = get_setting(conn, "password")
    return stored is not None and verify_password(stored, password)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO sessions(token_hash, created_at, expires_at, user_agent) VALUES (?,?,?,?)",
        (_token_hash(token), now, now + SESSION_TTL_S, (user_agent or "")[:200]),
    )
    return token


def session_valid(conn: sqlite3.Connection, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
    return bool(row) and row[0] > time.time()


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
