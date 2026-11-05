"""Run the dashboard (Uvicorn) and the scheduler together in one event loop."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Callable, Iterator

import uvicorn

from api_qovrix.errors import QovrixError
from api_qovrix.server.scheduler import Server
from api_qovrix.web.app import create_app
from api_qovrix.web.auth import Auth, is_loopback

MIN_PASSWORD_LENGTH = 8


class WebError(QovrixError):
    pass


def check_exposure(host: str, auth: Auth) -> list[str]:
    """Refuse an unprotected dashboard on a public address. Returns warnings for safe-but-notable setups."""
    if not is_loopback(host) and not auth.password:
        raise WebError(
            f"the dashboard listens on {host}, so it needs a password: set API_QOVRIX_PASSWORD "
            "(or listen on 127.0.0.1 only, or use --no-web)"
        )
    if auth.password and len(auth.password) < MIN_PASSWORD_LENGTH:
        raise WebError(f"API_QOVRIX_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters")
    warnings = []
    if auth.open:
        warnings.append(
            "dashboard has no password (fine on this machine only; set API_QOVRIX_PASSWORD to require one)"
        )
    if not auth.api_key and not auth.open:
        warnings.append("REST API: no API_QOVRIX_API_KEY set, so /api/v1 only works from a logged-in browser")
    return warnings


class _Uvicorn(uvicorn.Server):
    """Uvicorn without its own signal handling: `serve` stops the web server and scheduler together."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def bind(host: str, port: int) -> socket.socket:
    """Open the listening socket ourselves, so a busy port is a clear error (not an exit inside Uvicorn)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name != "nt":  # on Windows SO_REUSEADDR would let two processes share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise WebError(f"the dashboard cannot listen on {host}:{port}: {exc.strerror or exc}") from None
    sock.set_inheritable(True)
    return sock


def url_for(host: str, port: int) -> str:
    shown = "localhost" if host in ("127.0.0.1", "::1", "localhost") else host
    if ":" in shown:
        shown = f"[{shown}]"
    return f"http://{shown}:{port}"


async def serve_with_web(
    server: Server, auth: Auth, host: str, port: int, log: Callable[[str], None]
) -> None:
    app = create_app(server, auth)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
        server_header=False,
        proxy_headers=True,
    )
    sock = bind(host, port)
    web = _Uvicorn(config)
    scheduler = asyncio.create_task(server.serve())
    web_task = asyncio.create_task(web.serve(sockets=[sock]))
    shown = url_for(host, sock.getsockname()[1])
    log(f"dashboard: {shown}" + (" (all network interfaces)" if host in ("0.0.0.0", "::") else ""))  # noqa: S104

    try:
        done, _ = await asyncio.wait({scheduler, web_task}, return_when=asyncio.FIRST_COMPLETED)
        web_failed = web_task in done and not server.stopping
    finally:
        server.stop()
        web.should_exit = True
        results = await asyncio.gather(scheduler, web_task, return_exceptions=True)
        sock.close()
    if web_failed:
        detail = results[1]
        raise WebError(f"the dashboard stopped unexpectedly: {detail!r}")
