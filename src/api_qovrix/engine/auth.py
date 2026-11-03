"""Apply auth to a request. OAuth2 client-credentials tokens are cached until shortly before expiry."""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass

import httpx

from api_qovrix.config.models import ApiKeyAuth, Auth, BasicAuth, BearerAuth, OAuth2ClientCredentials
from api_qovrix.errors import QovrixError


class AuthError(QovrixError):
    pass


@dataclass
class _Token:
    value: str
    expires_at: float


class TokenCache:
    """Shared across a run (and later across scheduler ticks)."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, ...], _Token] = {}
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self.issued: set[str] = set()

    async def get(self, auth: OAuth2ClientCredentials, client: httpx.AsyncClient, timeout: float) -> str:
        key = (auth.token_url, auth.client_id, auth.scope or "", auth.audience or "")
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(key)
            if cached and cached.expires_at > time.monotonic():
                return cached.value
            token = await self._fetch(auth, client, timeout)
            self._tokens[key] = token
            self.issued.add(token.value)
            return token.value

    async def _fetch(
        self, auth: OAuth2ClientCredentials, client: httpx.AsyncClient, timeout: float
    ) -> _Token:
        data = {"grant_type": "client_credentials", **auth.extra_params}
        if auth.scope:
            data["scope"] = auth.scope
        if auth.audience:
            data["audience"] = auth.audience
        headers = {"Accept": "application/json"}
        if auth.client_auth == "basic":
            headers["Authorization"] = basic_header(auth.client_id, auth.client_secret)
        else:
            data["client_id"] = auth.client_id
            data["client_secret"] = auth.client_secret
        try:
            resp = await client.post(auth.token_url, data=data, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AuthError(f"OAuth2 token request failed: {type(exc).__name__}: {exc}") from None
        if resp.status_code >= 400:
            raise AuthError(f"OAuth2 token request returned {resp.status_code}")
        try:
            body = resp.json()
            value = body["access_token"]
        except (ValueError, KeyError, TypeError):
            raise AuthError("OAuth2 token response has no access_token") from None
        expires_in = body.get("expires_in")
        try:
            lifetime = float(expires_in) if expires_in is not None else 300.0
        except (TypeError, ValueError):
            lifetime = 300.0
        # Refresh 30 s early (or at 90 % of very short lifetimes).
        margin = min(30.0, lifetime * 0.1)
        return _Token(value=str(value), expires_at=time.monotonic() + max(lifetime - margin, 1.0))


def basic_header(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def apply_auth(
    auth: Auth | None,
    headers: dict[str, str],
    params: dict[str, object],
    tokens: TokenCache,
    client: httpx.AsyncClient,
    timeout: float,
) -> None:
    if auth is None:
        return
    if isinstance(auth, BearerAuth):
        headers["Authorization"] = f"Bearer {auth.token}"
    elif isinstance(auth, BasicAuth):
        headers["Authorization"] = basic_header(auth.username, auth.password)
    elif isinstance(auth, ApiKeyAuth):
        if auth.location == "header":
            headers[auth.name] = auth.value
        else:
            params[auth.name] = auth.value
    elif isinstance(auth, OAuth2ClientCredentials):
        token = await tokens.get(auth, client, timeout)
        headers["Authorization"] = f"Bearer {token}"
