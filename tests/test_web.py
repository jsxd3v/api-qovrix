import asyncio
import os
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from api_qovrix.cli.app import app as cli_app
from api_qovrix.config.loader import load_project
from api_qovrix.engine.runner import RunOptions
from api_qovrix.server import scheduler as sched
from api_qovrix.server.scheduler import Server, prepare_data_dir
from api_qovrix.server.storage import Storage
from api_qovrix.web.app import _safe_next, create_app
from api_qovrix.web.auth import COOKIE, LOGIN_ATTEMPTS, Auth, is_loopback
from api_qovrix.web.data import Chart, worst
from api_qovrix.web.server import WebError, bind, check_exposure, serve_with_web, url_for

PASSWORD = "correct-horse-battery"
API_KEY = "key-0123456789abcdef"
CONFIG = """
version: 1
project: shop
environments: { prod: { base_url: "https://api.test" } }
notifiers:
  chat: { type: webhook, url: "https://hooks.test/x" }
defaults: { notify: [chat], interval: 1m, alert_after: 2 }
"""
MONITORS = """
monitors:
  - name: orders
    description: Order list
    request: { url: /orders }
  - name: users
    request: { url: /users }
    assert: [{ jsonpath: $.ok, equals: true }]
"""


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "hooks.test":
        return httpx.Response(200)
    if request.url.path == "/orders":
        return httpx.Response(200, json=[{"id": 1}])
    return httpx.Response(200, json={"ok": False})


@pytest.fixture
def server(make_project, tmp_path):
    root = make_project(CONFIG, {"monitors/shop.yaml": MONITORS})
    project = load_project(root / "api-qovrix.yaml")
    storage = Storage(prepare_data_dir(tmp_path / "data"))
    srv = Server(project, storage, RunOptions(transport=httpx.MockTransport(handler)), log=lambda s: None)
    srv.alerts.transport = httpx.MockTransport(handler)
    yield srv
    storage.close()


def client(server, password=PASSWORD, api_key=API_KEY):
    return TestClient(create_app(server, Auth(password=password, api_key=api_key)))


def login(c):
    r = c.post("/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return r


# --------------------------------------------------------------------------- auth rules


def test_auth_helpers():
    a = Auth(password="pw-123456", api_key="k")
    token = a.make_session(now=1000)
    assert a.valid_session(token, now=1001)
    assert not a.valid_session(token, now=1000 + 13 * 3600)  # expired
    assert not a.valid_session(token[:-2] + "xx", now=1001)
    assert not a.valid_session("garbage", now=1001)
    assert not Auth(password="pw-123456", api_key=None).valid_session(token, now=1001)  # other secret
    assert a.check_bearer("Bearer k") and not a.check_bearer("Bearer x") and not a.check_bearer(None)
    assert not Auth(None, None).check_bearer("Bearer ")
    for i in range(LOGIN_ATTEMPTS):
        assert a.allow_login("1.2.3.4", now=float(i))
    assert not a.allow_login("1.2.3.4", now=20.0)
    assert a.allow_login("1.2.3.4", now=1000.0)  # window passed
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("example.com")
    assert _safe_next("/monitors/a") == "/monitors/a"
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "evil"):
        assert _safe_next(bad) == "/"


def test_exposure_rules():
    with pytest.raises(WebError, match="needs a password"):
        check_exposure("0.0.0.0", Auth(None, None))
    with pytest.raises(WebError, match="at least 8"):
        check_exposure("127.0.0.1", Auth("short", None))
    assert any("no password" in w for w in check_exposure("127.0.0.1", Auth(None, None)))
    assert any("API_QOVRIX_API_KEY" in w for w in check_exposure("0.0.0.0", Auth(PASSWORD, None)))
    assert check_exposure("0.0.0.0", Auth(PASSWORD, API_KEY)) == []
    assert url_for("127.0.0.1", 8080) == "http://localhost:8080"
    assert url_for("::", 80) == "http://[::]:80"


# --------------------------------------------------------------------------- pages


def test_login_flow_and_security_headers(server):
    with client(server) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
        r = c.get("/login")
        assert r.status_code == 200 and 'name="password"' in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"

        r = c.post("/login", data={"password": "nope", "next": "/"})
        assert "Wrong password." in r.text and COOKIE not in c.cookies

        r = login(c)
        set_cookie = r.headers["set-cookie"]
        assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
        assert c.get("/").status_code == 200
        r = c.post("/logout", follow_redirects=False)
        assert r.status_code == 303
        c.cookies.clear()
        assert c.get("/", follow_redirects=False).status_code == 303


def test_login_rate_limit(server):
    with client(server) as c:
        for _ in range(LOGIN_ATTEMPTS):
            c.post("/login", data={"password": "nope"})
        r = c.post("/login", data={"password": PASSWORD})
        assert "Too many attempts" in r.text


def test_cross_origin_post_refused(server):
    with client(server) as c:
        login(c)
        r = c.post("/monitors/orders/run", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = c.post("/monitors/orders/run", headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303


def test_pages_show_data(server):
    async def two_rounds():
        await server.run_once()
        await server.run_once()

    asyncio.run(two_rounds())
    with client(server, password=None) as c:  # open dashboard (localhost)
        r = c.get("/")
        assert r.status_code == 200
        assert "orders" in r.text and "users" in r.text and "Order list" in r.text
        assert 'badge down">down<' in r.text and "<polyline" in r.text and "Log out" not in r.text
        assert "$.ok equals true: expected true, got false" in r.text

        r = c.get("/monitors/users")
        assert r.status_code == 200 and "Run details" in r.text and "✘ $.ok equals true" in r.text
        assert "alert after 2 failures" in r.text and "notifies chat" in r.text
        first_run = r.text.split("?run=")[1].split('"')[0]
        r = c.get(f"/monitors/users?run={first_run}")
        assert r.status_code == 200 and 'class="selected"' in r.text and "data-refresh" not in r.text

        r = c.get("/incidents")
        assert r.status_code == 200 and 'badge down">open' in r.text and "users" in r.text

        assert c.get("/monitors/ghost").status_code == 404
        assert c.get("/static/app.css").status_code == 200
        assert c.get("/static/app.js").headers["content-type"].startswith("text/javascript")

        r = c.post("/monitors/orders/run", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/monitors/orders"


# --------------------------------------------------------------------------- API


def test_api(server):
    asyncio.run(server.run_once())
    with client(server) as c:
        assert c.get("/healthz").json()["status"] == "ok"
        r = c.get("/api/v1/services")
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        h = {"Authorization": f"Bearer {API_KEY}"}
        data = c.get("/api/v1/services", headers=h).json()
        assert data["services"][0]["name"] == "shop" and data["counts"]["up"] == 1
        mons = c.get("/api/v1/monitors?service=shop", headers=h).json()["monitors"]
        assert {m["name"] for m in mons} == {"orders", "users"}
        assert c.get("/api/v1/monitors?service=other", headers=h).json()["monitors"] == []
        one = c.get("/api/v1/monitors/users", headers=h).json()
        assert one["monitor"]["note"] == "failing 1/2" and len(one["runs"]) == 1
        run_id = one["runs"][0]["id"]
        details = c.get(f"/api/v1/runs/{run_id}", headers=h).json()
        assert details["steps"][0]["assertions"][0]["passed"] is False
        assert c.get("/api/v1/runs/999999", headers=h).status_code == 404
        assert len(c.get("/api/v1/monitors/users/runs?limit=5", headers=h).json()["runs"]) == 1
        r = c.post("/api/v1/monitors/users/run", headers=h).json()
        assert r["status"] == "fail"
        assert c.get("/api/v1/incidents?state=open", headers=h).json()["incidents"][0]["monitor"] == "users"
        assert c.get("/api/v1/monitors/ghost", headers=h).status_code == 404
        assert c.get("/api/v1/openapi.json", headers=h).status_code == 200
        assert c.get("/docs", follow_redirects=False).status_code == 303  # pages need a login
        login(c)
        assert c.get("/api/v1/services").status_code == 200  # browser session works too
        assert c.get("/docs").status_code == 404  # no interactive docs (they would load CDN scripts)


# --------------------------------------------------------------------------- charts


def test_chart_and_worst():
    runs = [{"duration_ms": d, "status": s} for d, s in [(30, "pass"), (10, "fail"), (20, "pass")]]
    c = Chart.build(runs, 100, 20)  # newest first in, oldest first drawn
    assert c.count == 3 and c.max_ms == 30 and len(c.failures) == 1
    assert c.points.split()[0].startswith("4.0,")
    assert Chart.build([{"duration_ms": 5, "status": "pass"}], 100, 20).points.startswith("50.0,")
    assert worst(["up", "down", "paused"]) == "down"
    assert worst(["up", "paused"]) == "up" and worst([]) == "unknown" and worst(["paused"]) == "paused"


# --------------------------------------------------------------------------- real server


def test_bind_busy_port():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        with pytest.raises(WebError, match="cannot listen"):
            bind("127.0.0.1", taken.getsockname()[1])
    finally:
        taken.close()


def test_serve_with_web_end_to_end(server, monkeypatch):
    monkeypatch.setattr(sched, "FIRST_RUN_SPREAD", 0.0)
    logs = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def go():
        task = asyncio.create_task(serve_with_web(server, Auth(None, None), "127.0.0.1", port, logs.append))
        body = None
        async with httpx.AsyncClient() as http:
            for _ in range(50):
                await asyncio.sleep(0.1)
                try:
                    r = await http.get(f"http://127.0.0.1:{port}/api/v1/services")
                except httpx.ConnectError:
                    continue
                body = r.json()
                if body["counts"]["unknown"] == 0:
                    break
        server.stop()
        await asyncio.wait_for(task, 10)
        return body

    body = asyncio.run(go())
    assert body["counts"] == {"up": 1, "down": 0, "unknown": 0, "paused": 0} or body["counts"]["up"] >= 1
    assert logs == [f"dashboard: http://localhost:{port}"]
    assert server.stopping


def test_cli_serve_refuses_public_without_password(make_project, monkeypatch):
    monkeypatch.delenv("API_QOVRIX_PASSWORD", raising=False)
    root = make_project(CONFIG, {"monitors/shop.yaml": MONITORS})
    old = os.getcwd()
    os.chdir(root)
    try:
        r = CliRunner().invoke(cli_app, ["serve", "--host", "0.0.0.0"])
        assert r.exit_code == 2 and "needs a password" in r.output
        monkeypatch.setenv("API_QOVRIX_PASSWORD", "short")
        r = CliRunner().invoke(cli_app, ["serve"])
        assert r.exit_code == 2 and "at least 8" in r.output
    finally:
        os.chdir(old)


def test_static_types_survive_a_bad_windows_registry(server):
    import mimetypes

    mimetypes.init()
    mimetypes.add_type("text/plain", ".js")  # what some Windows registries report
    mimetypes.add_type("text/plain", ".css")
    try:
        with client(server, password=None) as c:
            assert c.get("/static/app.js").headers["content-type"].startswith("text/javascript")
            assert c.get("/static/app.css").headers["content-type"].startswith("text/css")
            assert c.get("/static/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    finally:
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("text/css", ".css")


def test_cli_serve_reads_password_from_env_file(make_project, monkeypatch, tmp_path):
    monkeypatch.delenv("API_QOVRIX_PASSWORD", raising=False)
    root = make_project(CONFIG, {"monitors/shop.yaml": MONITORS})
    (root / ".env").write_text("API_QOVRIX_PASSWORD=short\n", encoding="utf-8")
    old = os.getcwd()
    os.chdir(root)
    try:
        r = CliRunner().invoke(cli_app, ["serve", "--env-file", ".env", "--host", "0.0.0.0"])
    finally:
        os.chdir(old)
        os.environ.pop("API_QOVRIX_PASSWORD", None)
    assert r.exit_code == 2 and "at least 8" in r.output  # the file's value was used


def test_browser_form_posts_are_accepted_and_cross_site_refused(server):
    """What real browsers send: Sec-Fetch-Site, and behind a proxy an Origin that differs from Host."""
    with client(server) as c:
        r = c.get("/login")
        assert r.headers["referrer-policy"] == "same-origin"  # no-referrer made browsers send Origin: null

        same = {"Sec-Fetch-Site": "same-origin", "Origin": "https://monitor.example.com"}
        r = c.post("/login", data={"password": PASSWORD, "next": "/"}, headers=same, follow_redirects=False)
        assert r.status_code == 303 and COOKIE in c.cookies  # behind a proxy, Host is the upstream address

        cross = {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}
        assert c.post("/monitors/orders/run", headers=cross).status_code == 403
        assert c.post("/logout", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403

        # older browsers without Sec-Fetch-Site: Origin compared with Host or the proxy's forwarded host
        assert c.post("/monitors/orders/run", headers={"Origin": "null"}).status_code == 403
        proxied = {"Origin": "https://monitor.example.com", "X-Forwarded-Host": "monitor.example.com"}
        r = c.post("/monitors/orders/run", headers=proxied, follow_redirects=False)
        assert r.status_code == 303
