"""HTTP execution with a timing breakdown (connect, TLS, time to first byte, total)."""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from api_qovrix import __version__
from api_qovrix.errors import QovrixError

USER_AGENT = f"api-qovrix/{__version__} (+https://github.com/jsxd3v/api-qovrix)"
MAX_BODY_BYTES = 10 * 1024 * 1024


class RequestError(QovrixError):
    pass


@dataclass
class HttpResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes
    elapsed_ms: float
    timings: dict[str, float]
    http_version: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def join_url(base_url: str | None, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    if not base_url:
        raise RequestError(
            f"'{url}' is a relative URL but no base_url is set (monitor, service or environment)"
        )
    return base_url.rstrip("/") + "/" + url.lstrip("/")


def check_host(url: str, allowed_hosts: list[str] | None) -> None:
    if allowed_hosts is None:
        return
    host = (urlsplit(url).hostname or "").lower()
    if not any(fnmatch.fnmatch(host, pattern.lower()) for pattern in allowed_hosts):
        raise RequestError(f"host '{host}' is not in allowed_hosts")


class HttpClients:
    """One AsyncClient per TLS-verification setting, shared by all monitors in a run."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._clients: dict[bool, httpx.AsyncClient] = {}

    def get(self, verify: bool = True) -> httpx.AsyncClient:
        if verify not in self._clients:
            kwargs: dict[str, Any] = {"headers": {"User-Agent": USER_AGENT}, "verify": verify}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            else:
                kwargs["http2"] = True
            self._clients[verify] = httpx.AsyncClient(**kwargs)
        return self._clients[verify]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


async def send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any],
    json_body: Any = None,
    body: str | None = None,
    form: dict[str, Any] | None = None,
    timeout: float,
    follow_redirects: bool,
) -> HttpResponse:
    marks: dict[str, float] = {}

    async def trace(event: str, info: dict[str, Any]) -> None:
        marks.setdefault(event, time.perf_counter())

    kwargs: dict[str, Any] = {"headers": headers, "params": params or None}
    if json_body is not None:
        kwargs["json"] = json_body
    elif body is not None:
        kwargs["content"] = body.encode()
    elif form is not None:
        kwargs["data"] = form

    request = client.build_request(method, url, timeout=timeout, extensions={"trace": trace}, **kwargs)
    start = time.perf_counter()
    try:
        response = await client.send(request, follow_redirects=follow_redirects, stream=True)
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise RequestError(f"response body is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
        finally:
            await response.aclose()
    except httpx.TimeoutException:
        raise RequestError(f"timed out after {timeout:g}s") from None
    except httpx.ConnectError as exc:
        raise RequestError(f"connection failed: {exc or 'could not connect'}") from None
    except httpx.TooManyRedirects:
        raise RequestError("too many redirects") from None
    except httpx.HTTPError as exc:
        raise RequestError(f"{type(exc).__name__}: {exc}") from None
    end = time.perf_counter()

    timings = {"total": (end - start) * 1000}

    def span(a: str, b: str) -> float | None:
        if a in marks and b in marks:
            return (marks[b] - marks[a]) * 1000
        return None

    if (v := span("connection.connect_tcp.started", "connection.connect_tcp.complete")) is not None:
        timings["connect"] = v
    if (v := span("connection.start_tls.started", "connection.start_tls.complete")) is not None:
        timings["tls"] = v
    for proto in ("http11", "http2"):
        key = f"{proto}.receive_response_headers.complete"
        if key in marks:
            timings["ttfb"] = (marks[key] - start) * 1000
            break

    return HttpResponse(
        status_code=response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        elapsed_ms=timings["total"],
        timings={k: round(v, 1) for k, v in timings.items()},
        http_version=response.http_version,
    )
