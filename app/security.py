"""Password hashing, sessions, rate limiting and abusive-IP blocking."""

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from . import config, db

log = logging.getLogger("silknet.security")

# ---------------------------------------------------------------- passwords

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


# Verified against when the email doesn't exist, so response time doesn't reveal which emails are registered.
DUMMY_HASH = hash_password(secrets.token_hex(16))

# ---------------------------------------------------------------- sessions

SESSION_COOKIE = "silknet_session"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = time.time() + config.SESSION_DAYS * 86400
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_token_hash(token), user_id, expires),
    )
    return token


def user_for_token(token: str):
    with db.connect() as conn:
        return conn.execute(
            "SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (_token_hash(token), time.time()),
        ).fetchone()


def delete_session(token: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def purge_expired_sessions() -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))


# ---------------------------------------------------------------- client IP


def client_ip(request: Request) -> str:
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- rate limiting


class RateLimiter:
    """In-memory sliding-window limiter. Fine for a single process; use Redis if you scale out."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next hit would be allowed."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= now - window:
                dq.popleft()
            if len(dq) >= limit:
                return max(dq[0] + window - now, 1.0)
            dq.append(now)
            if len(self._hits) > 50_000:  # keep memory bounded
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return None


# ---------------------------------------------------------------- IP blocking


class IpGuard:
    """Counts 'strikes' per IP; too many in a window blocks the IP. Blocks persist in SQLite
    (so they survive restarts and can be lifted with `python -m app.admin unblock <ip>`)."""

    REFRESH_SECONDS = 15

    def __init__(self) -> None:
        self._strikes: dict[str, deque] = defaultdict(deque)
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_refresh = 0.0

    def refresh(self) -> None:
        """Reload blocks from the DB so changes made by the admin CLI take effect."""
        with db.connect() as conn:
            rows = conn.execute("SELECT ip, until FROM ip_blocks WHERE until > ?", (time.time(),)).fetchall()
        fresh = {r["ip"]: r["until"] for r in rows}
        with self._lock:
            for ip in set(self._blocked) - set(fresh):  # unblocked externally -> forgive past strikes
                self._strikes.pop(ip, None)
            self._blocked = fresh
            self._last_refresh = time.monotonic()

    def is_blocked(self, ip: str) -> bool:
        if ip in config.IP_ALLOWLIST:
            return False
        if time.monotonic() - self._last_refresh > self.REFRESH_SECONDS:
            self.refresh()
        until = self._blocked.get(ip)
        return bool(until and until > time.time())

    def strike(self, ip: str, reason: str, weight: int = 1) -> None:
        if ip in config.IP_ALLOWLIST:
            return
        now = time.monotonic()
        with self._lock:
            dq = self._strikes[ip]
            dq.extend([now] * weight)
            while dq and dq[0] <= now - config.STRIKE_WINDOW:
                dq.popleft()
            should_block = len(dq) >= config.STRIKE_LIMIT and ip not in self._blocked
            if should_block:
                until = time.time() + config.BLOCK_SECONDS
                self._blocked[ip] = until
                dq.clear()
        if should_block:
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO ip_blocks (ip, until, reason) VALUES (?, ?, ?) "
                    "ON CONFLICT(ip) DO UPDATE SET until = excluded.until, reason = excluded.reason",
                    (ip, until, reason),
                )
            log.warning("Blocked IP %s for %ss (last strike: %s)", ip, config.BLOCK_SECONDS, reason)


limiter = RateLimiter()
guard = IpGuard()
