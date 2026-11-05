"""Apply auth to a request. OAuth2 client-credentials tokens are cached until shortly before expiry."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from api_qovrix.config.models import (
    ApiKeyAuth,
    Auth,
    BasicAuth,
    BearerAuth,
    OAuth2ClientCredentials,
    TokenRequestAuth,
)
from api_qovrix.engine.jsonpaths import MISSING, query
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
        self.logins = 0

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

    async def get_requested(self, auth: TokenRequestAuth, client: httpx.AsyncClient, timeout: float) -> str:
        """Token from a login request, shared by every monitor that uses the same (rendered) login."""
        r = auth.request
        identity = json.dumps(
            [r.method.upper(), r.url, r.headers, r.params, r.json_body, r.form, auth.token],
            sort_keys=True,
            default=str,
        )
        key = ("token_request", hashlib.sha256(identity.encode()).hexdigest())
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(key)
            if cached and cached.expires_at > time.monotonic():
                return cached.value
            token = await self._fetch_requested(auth, client, timeout)
            self._tokens[key] = token
            self.issued.add(token.value)
            self.logins += 1
            return token.value

    async def _fetch_requested(
        self, auth: TokenRequestAuth, client: httpx.AsyncClient, timeout: float
    ) -> _Token:
        r = auth.request
        kwargs: dict[str, Any] = {"headers": {"Accept": "application/json", **r.headers}}
        if r.params:
            kwargs["params"] = {k: str(v) for k, v in r.params.items()}
        if r.json_body is not None:
            kwargs["json"] = r.json_body
        elif r.form is not None:
            kwargs["data"] = r.form
        try:
            resp = await client.request(r.method.upper(), r.url, timeout=r.timeout or timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise AuthError(f"login request failed: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise AuthError(f"login request returned {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise AuthError("login response is not JSON") from None
        value = query(body, auth.token)
        if value is MISSING or not isinstance(value, str | int) or isinstance(value, bool) or value == "":
            raise AuthError(f"login response has no token at {auth.token}")
        lifetime = auth.lifetime
        if auth.expires_in:
            raw = query(body, auth.expires_in)
            with contextlib.suppress(TypeError, ValueError):
                lifetime = float(raw) if raw is not MISSING else lifetime
        margin = min(30.0, lifetime * 0.1)
        return _Token(value=str(value), expires_at=time.monotonic() + max(lifetime - margin, 1.0))

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
    elif isinstance(auth, TokenRequestAuth):
        token = await tokens.get_requested(auth, client, timeout)
        headers[auth.header] = f"{auth.prefix}{token}"
