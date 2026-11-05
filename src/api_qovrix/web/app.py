"""The dashboard (server-rendered HTML) and REST API, served by `api-qovrix serve`."""

from __future__ import annotations

import asyncio
import mimetypes
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from api_qovrix import __version__
from api_qovrix.config.loader import logo_path
from api_qovrix.server.scheduler import Server
from api_qovrix.server.storage import now_iso
from api_qovrix.web import data
from api_qovrix.web import status as status_data
from api_qovrix.web.auth import COOKIE, SESSION_HOURS, Auth

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
PUBLIC_PATHS = ("/login", "/healthz", "/static/", "/brand.css", "/brand/logo")
STATUS_PATHS = ("/status", "/status.json")
STATUS_CACHE_SECONDS = 10.0
LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp"}  # fmt: skip


STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}


def _fix_static_types() -> None:
    """Windows can map .js/.css to text/plain in the registry; with `nosniff`, browsers would refuse them."""
    mimetypes.init()
    for ext, mime in STATIC_TYPES.items():
        mimetypes.add_type(mime, ext)


def _web_dir() -> Path:
    return Path(str(resources.files("api_qovrix.web")))


def _templates() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_web_dir() / "templates")),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["version"] = __version__
    return env


def create_app(server: Server, auth: Auth, clock: Any = None) -> FastAPI:
    storage = server.storage  # server.project is read on every request: it changes when monitors reload
    now = clock or (lambda: datetime.now(UTC).replace(microsecond=0))
    templates = _templates()
    app = FastAPI(
        title="api-qovrix",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    _fix_static_types()
    app.mount("/static", StaticFiles(directory=str(_web_dir() / "static")), name="static")

    # ------------------------------------------------------------------ middleware

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if request.method == "POST" and not _same_origin(request):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        authed = auth.open or auth.valid_session(request.cookies.get(COOKIE))
        api_ok = authed or auth.check_bearer(request.headers.get("authorization"))
        public_status = path in STATUS_PATHS and server.project.config.status_page.enabled
        if not path.startswith(PUBLIC_PATHS) and not public_status:
            if path.startswith("/api/"):
                if not api_ok:
                    return JSONResponse(
                        {"detail": "authentication required"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            elif not authed:
                return RedirectResponse(f"/login?next={path}", status_code=303)
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # same-origin (not no-referrer): with no-referrer, browsers send `Origin: null` on our own form posts.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if public_status:
            response.headers["Cache-Control"] = "public, max-age=30"
        elif not path.startswith(("/static/", "/brand")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def brand() -> dict[str, Any]:
        cfg = server.project.config
        return {
            "name": cfg.branding.name or cfg.project or "api-qovrix",
            "logo": logo_path(server.project) is not None,
            "accent": cfg.branding.accent,
        }

    def render(name: str, request: Request, **ctx: Any) -> HTMLResponse:
        page = templates.get_template(name).render(
            request=request,
            project=server.project.config.project or "api-qovrix",
            brand=brand(),
            environment=server.project.environment,
            login_required=not auth.open,
            status_enabled=server.project.config.status_page.enabled,
            path=request.url.path,
            **ctx,
        )
        return HTMLResponse(page)

    # ------------------------------------------------------------------ branding

    @app.get("/brand.css")
    async def brand_css() -> Response:
        accent = server.project.config.branding.accent  # validated: #rrggbb
        css = f":root {{ --accent: {accent}; }}\n"
        return Response(css, media_type="text/css", headers={"Cache-Control": "public, max-age=60"})

    @app.get("/brand/logo")
    async def brand_logo() -> Response:
        path = logo_path(server.project)
        if path is None or not path.is_file():
            raise HTTPException(404, "no logo configured")
        return FileResponse(
            path,
            media_type=LOGO_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            headers={"Cache-Control": "public, max-age=300"},
        )

    # ------------------------------------------------------------------ public status page

    cache: dict[str, Any] = {"at": 0.0, "view": None, "project": None}

    async def status_view() -> status_data.StatusView:
        clock = asyncio.get_running_loop().time()
        if (
            cache["view"] is None
            or cache["project"] is not server.project
            or clock - cache["at"] > STATUS_CACHE_SECONDS
        ):
            cache["view"] = await status_data.build(server.project, storage, now())
            cache["at"], cache["project"] = clock, server.project
        view: status_data.StatusView = cache["view"]
        return view

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> Response:
        if not server.project.config.status_page.enabled:
            raise HTTPException(404, "the status page is not enabled")
        return render("status.html", request, s=await status_view(), refresh=True, refresh_seconds=60)

    @app.get("/status.json")
    async def status_json() -> dict[str, Any]:
        if not server.project.config.status_page.enabled:
            raise HTTPException(404, "the status page is not enabled")
        return (await status_view()).api()

    def find(name: str) -> Any:
        for m in server.project.monitors:
            if m.name == name:
                return m
        raise HTTPException(404, f"no monitor named '{name}'")

    # ------------------------------------------------------------------ login

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Response:
        if auth.open:
            return RedirectResponse("/", status_code=303)
        return render("login.html", request, next=_safe_next(next), error=None)

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        password = (form.get("password") or [""])[0]
        target = _safe_next((form.get("next") or ["/"])[0])
        client = request.client.host if request.client else "unknown"
        if not auth.allow_login(client):
            return render("login.html", request, next=target, error="Too many attempts. Wait a few minutes.")
        if not auth.check_password(password):
            await asyncio.sleep(0.5)
            return render("login.html", request, next=target, error="Wrong password.")
        auth.reset_login(client)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE,
            auth.make_session(),
            max_age=SESSION_HOURS * 3600,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    async def overview_page(request: Request) -> Response:
        services = await data.overview(server.project, storage, now())
        incidents = await storage.incidents(open_only=True)
        return render(
            "overview.html",
            request,
            services=services,
            counts=data.counts(services),
            banner=data.banner(services),
            incidents=incidents,
            refresh=True,
        )

    @app.get("/monitors/{name}", response_class=HTMLResponse)
    async def monitor_page(request: Request, name: str, run: int | None = None) -> Response:
        m = find(name)
        current = now()
        runs = await storage.history(name, 100)
        states = {s.monitor: s for s in await storage.all_states()}
        uptime = await storage.uptime(data.ago_iso(current, hours=24))
        view = await data.monitor_view(m, states.get(name), uptime, current, runs, chart_size=(720, 160))
        selected = run if run is not None else (runs[0]["id"] if runs else None)
        details = await storage.run_details(selected) if selected is not None else None
        for r in runs:
            r["ago"] = data.ago(r["started_at"], current)
        incidents = [i for i in await storage.incidents(limit=200) if i.monitor == name][:10]
        daily = await storage.daily((current - timedelta(days=29)).date().isoformat(), [name])
        return render(
            "monitor.html",
            request,
            days=data.day_segments(daily, current),
            m=view,
            config=m,
            runs=runs[:25],
            details=details,
            selected=selected,
            incidents=incidents,
            refresh=run is None,
        )

    @app.post("/monitors/{name}/run")
    async def run_now_page(name: str) -> Response:
        m = find(name)
        await server.check(m)
        await server.alerts.flush()
        return RedirectResponse(f"/monitors/{name}", status_code=303)

    @app.get("/incidents", response_class=HTMLResponse)
    async def incidents_page(request: Request) -> Response:
        current = now()
        items = []
        for i in await storage.incidents(limit=200):
            end = datetime.fromisoformat(i.resolved_at) if i.resolved_at else current
            items.append(
                {
                    "incident": i,
                    "opened": data.ago(i.opened_at, current),
                    "duration": data.human_duration(
                        (end - datetime.fromisoformat(i.opened_at)).total_seconds()
                    ),
                }
            )
        return render("incidents.html", request, items=items)

    # ------------------------------------------------------------------ REST API

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """For uptime checks from outside: 503 when the scheduler is not running."""
        health = server.health() if server.started_at else {"status": "ok"}
        body = {**health, "version": __version__, "time": now_iso()}
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @app.get("/api/v1/services")
    async def api_services() -> dict[str, Any]:
        services = await data.overview(server.project, storage, now(), charts=False)
        return {"services": [s.api() for s in services], "counts": data.counts(services)}

    @app.get("/api/v1/monitors")
    async def api_monitors(service: str | None = None) -> dict[str, Any]:
        services = await data.overview(server.project, storage, now(), charts=False)
        return {"monitors": [m.api() for s in services for m in s.monitors if service in (None, s.name)]}

    @app.get("/api/v1/monitors/{name}")
    async def api_monitor(name: str) -> dict[str, Any]:
        find(name)
        services = await data.overview(server.project, storage, now(), charts=False)
        view = next(m for s in services for m in s.monitors if m.name == name)
        return {"monitor": view.api(), "runs": await storage.history(name, 20)}

    @app.get("/api/v1/monitors/{name}/runs")
    async def api_runs(name: str, limit: int = 50) -> dict[str, Any]:
        find(name)
        return {"runs": await storage.history(name, max(1, min(limit, 1000)))}

    @app.get("/api/v1/runs/{run_id}")
    async def api_run(run_id: int) -> dict[str, Any]:
        details = await storage.run_details(run_id)
        if details is None:
            raise HTTPException(404, "run not found")
        return details

    @app.post("/api/v1/monitors/{name}/run")
    async def api_run_now(name: str) -> dict[str, Any]:
        m = find(name)
        result = await server.check(m)
        await server.alerts.flush()
        return {"status": result.status, "summary": result.failure_summary}

    @app.get("/api/v1/incidents")
    async def api_incidents(state: str = "all") -> dict[str, Any]:
        items = await storage.incidents(open_only=state == "open", limit=500)
        return {"incidents": [i.__dict__ for i in items]}

    return app


def _same_origin(request: Request) -> bool:
    """Refuse cross-site form posts (defence in depth on top of SameSite cookies).

    Modern browsers say where a request comes from in `Sec-Fetch-Site`, which also works behind a reverse
    proxy. Older browsers: compare `Origin`/`Referer` with the host, including the proxy's forwarded host.
    """
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # non-browser clients (curl, scripts with an API key)
    if origin == "null":
        return False
    hosts = {request.headers.get("host", "")}
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        hosts.add(forwarded.split(",")[0].strip())
    return urlsplit(origin).netloc in hosts


def _safe_next(target: str) -> str:
    """Only allow redirects to local paths after login."""
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
