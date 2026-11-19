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
import threading
import time

from .db import get_setting, set_setting

SESSION_COOKIE = "stezhka_session"
SESSION_TTL_S = 30 * 24 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # ~16 MiB, ~50 ms on an old i3
MIN_PASSWORD_LEN = 12


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


class LoginThrottle:
    """In-memory brute-force protection for the single login.

    * attempts are checked one at a time (with a 1 s delay on failure), so parallel connections
      cannot multiply the guessing rate;
    * an address with PER_IP_MAX failures within WINDOW_S is refused for WINDOW_S;
    * GLOBAL_MAX failures from all addresses within WINDOW_S lock every login for WINDOW_S
      (covers distributed guessing, and addresses hidden behind a CDN). The owner can lift that
      early by restarting the service.

    State is per process and resets on restart; that is acceptable for one user.
    """

    PER_IP_MAX = 5
    GLOBAL_MAX = 30
    WINDOW_S = 15 * 60

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._by_ip: dict[str, list[float]] = {}
        self._all: list[float] = []
        self.lock = threading.Lock()  # held by the caller around check + verify

    def _recent(self, times: list[float]) -> list[float]:
        cutoff = self._clock() - self.WINDOW_S
        return [t for t in times if t > cutoff]

    def blocked(self, ip: str) -> bool:
        self._all = self._recent(self._all)
        mine = self._by_ip[ip] = self._recent(self._by_ip.get(ip, []))
        return len(mine) >= self.PER_IP_MAX or len(self._all) >= self.GLOBAL_MAX

    def failed(self, ip: str) -> None:
        now = self._clock()
        self._by_ip.setdefault(ip, []).append(now)
        self._all.append(now)
        if len(self._by_ip) > 10_000:  # bound memory under a wide scan
            self._by_ip = {k: v for k, v in self._by_ip.items() if self._recent(v)}

    def succeeded(self, ip: str) -> None:
        self._by_ip.pop(ip, None)


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


# --- upload keys -------------------------------------------------------------------------------
# A key can only upload rides (POST /api/rides with "Authorization: Bearer <key>"). It is shown
# once at creation; only its SHA-256 is stored. 256 random bits: guessing is not a concern.

UPLOAD_TOKEN_PREFIX = "stz_up_"


def create_upload_token(conn: sqlite3.Connection, name: str) -> tuple[int, str]:
    token = UPLOAD_TOKEN_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO upload_tokens(name, token_hash, created_at) VALUES (?, ?, ?)",
        (name, _token_hash(token), time.time()),
    )
    return cur.lastrowid, token


def upload_token_valid(conn: sqlite3.Connection, token: str | None) -> bool:
    if not token or not token.startswith(UPLOAD_TOKEN_PREFIX):
        return False
    cur = conn.execute(
        "UPDATE upload_tokens SET last_used_at = ? WHERE token_hash = ?", (time.time(), _token_hash(token))
    )
    return cur.rowcount == 1


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return None


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
