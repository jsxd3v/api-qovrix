"""Dashboard login (password + signed session cookie) and REST API keys.

- The dashboard password comes from API_QOVRIX_PASSWORD, the API key from API_QOVRIX_API_KEY.
- Without a password the dashboard is open, which is only allowed when listening on localhost.
- Sessions are HMAC-signed cookies (HttpOnly, SameSite=Lax) and end after SESSION_HOURS or on restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

COOKIE = "api_qovrix_session"
SESSION_HOURS = 12
LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 300.0


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass
class Auth:
    password: str | None
    api_key: str | None
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    _attempts: dict[str, deque[float]] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        """No password: the dashboard needs no login (localhost only)."""
        return not self.password

    # ------------------------------------------------------------------ sessions

    def make_session(self, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + SESSION_HOURS * 3600)
        payload = str(expires).encode()
        sig = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + b"." + sig).decode().rstrip("=")

    def valid_session(self, token: str | None, now: float | None = None) -> bool:
        if not token:
            return False
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload, sig = raw.split(b".", 1)
            expected = hmac.new(self.secret, payload, hashlib.sha256).digest()
            return hmac.compare_digest(sig, expected) and int(payload) > (time.time() if now is None else now)
        except (ValueError, TypeError):
            return False

    def check_password(self, given: str) -> bool:
        return bool(self.password) and _equal(given, self.password or "")

    # ------------------------------------------------------------------ API keys

    def check_bearer(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        return bool(self.api_key) and _equal(token, self.api_key or "")

    # ------------------------------------------------------------------ rate limit

    def allow_login(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        attempts = self._attempts.setdefault(client, deque())
        while attempts and now - attempts[0] > LOGIN_WINDOW:
            attempts.popleft()
        if len(attempts) >= LOGIN_ATTEMPTS:
            return False
        attempts.append(now)
        return True

    def reset_login(self, client: str) -> None:
        self._attempts.pop(client, None)
