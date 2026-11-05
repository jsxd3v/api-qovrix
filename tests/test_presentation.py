"""v0.4: public status page, dashboard redesign data, branding, and rich alert formats."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from api_qovrix.alerts import formats
from api_qovrix.alerts.message import Alert, FailedMonitor, RecoveredMonitor
from api_qovrix.alerts.notifiers import send_webhook
from api_qovrix.config.loader import load_project
from api_qovrix.config.models import WebhookNotifier
from api_qovrix.engine.results import MonitorResult, StepResult
from api_qovrix.engine.runner import RunOptions
from api_qovrix.errors import ConfigError
from api_qovrix.server.scheduler import Server, prepare_data_dir
from api_qovrix.server.storage import MonitorState, Storage
from api_qovrix.web import data
from api_qovrix.web import status as status_data
from api_qovrix.web.app import create_app
from api_qovrix.web.auth import COOKIE, Auth

EVIL = "<script>alert(1)</script>"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
CONFIG = """
version: 1
project: shop
environments: { prod: { base_url: "https://api.test" } }
server: { public_url: "https://monitor.example.com" }
branding: { name: Example Co, logo: logo.svg, accent: "#0e7490" }
status_page:
  enabled: true
  description: Live status.
  services:
    - { service: web, name: Website }
    - { service: reports, name: Reports API, description: Monthly reports }
  notices:
    - title: Planned database upgrade
      message: Reports may be slow.
      status: maintenance
      started: "2026-11-01T08:00:00Z"
      services: [reports]
    - title: Old notice
      status: resolved
      started: "2026-11-01T10:00:00Z"
      resolved: "2026-11-01T11:30:00Z"
"""
MONITORS = {
    "monitors/web.yaml": "monitors:\n  - name: home\n    request: { url: /home }\n",
    "monitors/reports.yaml": (
        "monitors:\n  - name: monthly\n    request: { url: /monthly }\n"
        "  - name: secret-internal-check\n    request: { url: /internal/token-check }\n"
    ),
    "monitors/internal.yaml": "monitors:\n  - name: hidden\n    request: { url: /hidden }\n",
}


@pytest.fixture
def root(make_project):
    r = make_project(CONFIG, MONITORS)
    (r / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>', encoding="utf-8"
    )
    return r


def result(name, service, status="pass", summary=""):
    step = StepResult("main", "GET", f"https://api.test/{name}", status_code=200 if status == "pass" else 500)
    r = MonitorResult(name, service, status, steps=[step], duration_ms=10.0)
    if summary:
        step.error = summary
    return r


# --------------------------------------------------------------------------- config checks


@pytest.mark.parametrize(
    "change,message",
    [
        (("service: web, name: Website", "service: nope, name: Website"), "no monitors use service 'nope'"),
        (("logo: logo.svg", "logo: missing.png"), "branding.logo not found"),
        (("logo: logo.svg", "logo: logo.gif"), "branding.logo must be one of"),
        (('public_url: "https://monitor.example.com"', 'public_url: "monitor.example.com"'), "public_url"),
        (('started: "2026-11-01T08:00:00Z"', 'started: "2026-11-01T08:00:00"'), "add a time zone"),
        (("services: [reports]", "services: [hidden]"), "not on the status page"),
        (('accent: "#0e7490"', 'accent: "teal"'), "accent"),
    ],
)
def test_presentation_config_errors(root, change, message):
    path = root / "api-qovrix.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(*change), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_project(path)


def test_big_logo_and_enabled_without_services(root):
    (root / "logo.svg").write_bytes(b"<svg>" + b" " * 600_000 + b"</svg>")
    with pytest.raises(ConfigError, match="larger than"):
        load_project(root / "api-qovrix.yaml")
    (root / "logo.svg").write_text("<svg/>", encoding="utf-8")
    text = (root / "api-qovrix.yaml").read_text(encoding="utf-8")
    start = text.index("  services:")
    (root / "api-qovrix.yaml").write_text(
        text[:start] + "  services: []\n" + "  notices: []\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="lists no services"):
        load_project(root / "api-qovrix.yaml")


# --------------------------------------------------------------------------- storage


def test_daily_summaries_hourly_and_v1_migration(tmp_path):
    path = prepare_data_dir(tmp_path / "d")
    s = Storage(path)

    async def go():
        await s.record_run(result("a", "web"), None, "2026-11-04T10:00:00+00:00")
        await s.record_run(result("a", "web", "fail"), None, "2026-11-04T10:30:00+00:00")
        await s.record_run(result("a", "web", "skipped"), None, "2026-11-05T09:00:00+00:00")
        await s.record_run(result("a", "web"), None, "2026-11-05T11:15:00+00:00")
        rows = await s.daily("2026-11-01")
        assert [(r["day"], r["ok"], r["total"]) for r in rows] == [("2026-11-04", 1, 2), ("2026-11-05", 1, 1)]
        hours = await s.hourly("2026-11-05T00")
        assert hours == {"a": {"2026-11-05T11": (1, 1)}}
        assert await s.daily("2026-11-01", ["other"]) == []

    asyncio.run(go())
    s.close()

    # an old (v1) database: no daily table data yet -> built from runs on open
    c = sqlite3.connect(path)
    c.execute("DELETE FROM daily")
    c.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    c.commit()
    c.close()
    s = Storage(path)
    rows = asyncio.run(s.daily("2026-11-01"))
    assert [(r["day"], r["ok"], r["total"]) for r in rows] == [("2026-11-04", 1, 2), ("2026-11-05", 1, 1)]
    s.close()
    c = sqlite3.connect(path)
    assert c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
    c.close()


# --------------------------------------------------------------------------- status page data


def make(root, tmp_path):
    project = load_project(root / "api-qovrix.yaml")
    storage = Storage(prepare_data_dir(tmp_path / "data"))
    return project, storage


def test_status_view_states_bars_incidents_and_privacy(root, tmp_path):
    project, storage = make(root, tmp_path)

    async def go():
        await storage.save_state(MonitorState("home", "web", state="up"))
        await storage.save_state(MonitorState("monthly", "reports", state="down"))
        await storage.save_state(MonitorState("secret-internal-check", "reports", state="up"))
        await storage.save_state(MonitorState("hidden", "internal", state="down"))
        for day, ok in (("2026-11-03", True), ("2026-11-04", False), ("2026-11-05", True)):
            await storage.record_run(
                result("home", "web", "pass" if ok else "fail"), None, f"{day}T08:00:00+00:00"
            )
        # two overlapping outages of the same public service -> one public incident
        await storage.open_incident("monthly", "reports", "2026-11-05T09:00:00+00:00", f"token=abc {EVIL}")
        await storage.open_incident("secret-internal-check", "reports", "2026-11-05T09:10:00+00:00", "x")
        await storage.resolve_incident("secret-internal-check", "2026-11-05T09:20:00+00:00")
        await storage.open_incident("hidden", "internal", "2026-11-05T09:00:00+00:00", "private")
        return await status_data.build(project, storage, NOW)

    view = asyncio.run(go())
    storage.close()
    assert (
        view.title == "Example Co status"
        and view.state == "partial_outage"
        and view.headline == "Partial outage"
    )
    states = {s.name: s.state for s in view.services}
    assert states == {
        "Website": "operational",
        "Reports API": "partial_outage",
    }  # internal service not listed
    web = view.services[0]
    assert len(web.bars) == 90 and [b.level for b in web.bars[-3:]] == ["up", "down", "up"]
    assert web.uptime == "66.67%"
    assert [i.title for i in view.active] == ["Reports API outage", "Planned database upgrade"]
    assert view.active[0].services == ["Reports API"] and view.active[1].status == "Maintenance"
    assert [i.title for i in view.history] == ["Old notice"] and view.history[0].duration == "1h 30m"
    dumped = json.dumps(view.api())
    for private in ("monthly", "secret-internal-check", "hidden", "token", "api.test", "<script", "internal"):
        assert private not in dumped, private


def test_maintenance_and_overall_operational(root, tmp_path):
    project, storage = make(root, tmp_path)

    async def go():
        await storage.save_state(MonitorState("home", "web", state="up"))
        await storage.save_state(MonitorState("monthly", "reports", state="up"))
        await storage.save_state(MonitorState("secret-internal-check", "reports", state="up"))
        during = await status_data.build(project, storage, NOW)
        project.config.status_page.notices = []
        after = await status_data.build(project, storage, NOW)
        return during, after

    during, after = asyncio.run(go())
    storage.close()
    assert during.state == "maintenance" and during.services[1].state == "maintenance"
    assert after.state == "operational" and after.headline == "All systems operational"


# --------------------------------------------------------------------------- web pages and branding


def app_client(root, tmp_path, password="correct-horse-battery"):
    project = load_project(root / "api-qovrix.yaml")
    storage = Storage(prepare_data_dir(tmp_path / "data"))
    server = Server(
        project, storage, RunOptions(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    return TestClient(create_app(server, Auth(password=password, api_key=None))), server, storage


def test_status_page_is_public_and_hides_internals(root, tmp_path):
    c, server, storage = app_client(root, tmp_path)
    asyncio.run(storage.save_state(MonitorState("monthly", "reports", state="down")))
    asyncio.run(storage.open_incident("monthly", "reports", NOW.isoformat(), f"boom {EVIL}"))
    with c:
        assert c.get("/", follow_redirects=False).status_code == 303  # dashboard still needs a login
        r = c.get("/status")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=30"
        assert "Example Co" in r.text and "Website" in r.text and "Reports API" in r.text
        assert "Planned database upgrade" in r.text and "Reports may be slow." in r.text
        for private in ("monthly", "secret-internal-check", "/internal/", "boom", "<script>alert", "Log out"):
            assert private not in r.text, private
        assert "default-src 'self'" in r.headers["content-security-policy"]
        j = c.get("/status.json").json()
        assert j["state"] == "partial_outage" and {s["name"] for s in j["services"]} == {
            "Website",
            "Reports API",
        }
        assert c.get("/brand.css").text.strip() == ":root { --accent: #0e7490; }"
        logo = c.get("/brand/logo")
        assert logo.status_code == 200 and logo.headers["content-type"].startswith("image/svg+xml")
        login = c.get("/login").text
        assert "Example Co" in login and 'href="/status"' in login and "/brand/logo" in login

        server.project.config.status_page.enabled = False
        assert c.get("/status", follow_redirects=False).status_code in (303, 404)
    storage.close()


def test_dashboard_banner_failing_badge_bars_and_error_box(root, tmp_path):
    c, _server, storage = app_client(root, tmp_path, password=None)

    async def setup():
        now = datetime.now(UTC).replace(microsecond=0)
        for i in range(3):
            ts = (now - timedelta(minutes=10 - i)).isoformat()
            await storage.record_run(result("home", "web", "fail", f"connection failed {EVIL}"), None, ts)
            await storage.record_run(result("monthly", "reports", "fail", "timed out"), None, ts)
        home = MonitorState("home", "web", state="up", consecutive_failures=1, last_status="fail")
        home.last_summary = f"connection failed {EVIL}"
        await storage.save_state(home)
        monthly = MonitorState("monthly", "reports", state="down", consecutive_failures=3, last_status="fail")
        monthly.last_summary = "timed out"
        await storage.save_state(monthly)

    asyncio.run(setup())
    with c:
        page = c.get("/").text
        assert "1 monitor is down" in page and "also failing: home" in page
        assert 'class="badge failing">failing 1/2' in page and 'class="badge down">down' in page
        assert 'class="errbox warn"' in page and 'class="errbox"' in page
        assert EVIL not in page and "&lt;script&gt;" in page
        assert page.count('class="seg ') >= 24 and 'title="' in page
        assert 'id="filter"' in page and 'data-filter="failing"' in page
        assert '<details class="service" open data-service="reports">' in page
        monitor = c.get("/monitors/monthly").text
        assert "Uptime, last 30 days" in monitor and monitor.count('class="seg ') == 30
        counts = c.get("/api/v1/services").json()["counts"]
        assert counts["failing"] == 1 and counts["down"] == 1 and counts["waiting"] == 2  # 2 never checked
    storage.close()


def test_banner_texts():
    def mv(name, state, failing=0):
        common = ("1 min", "every 1 min", "now", None, None, None, None, "—", [])
        return data.MonitorView(name, "s", None, state, state, *common, failing=failing)

    svc = [data.ServiceView("s", "up", [mv("a", "up"), mv("b", "up")])]
    assert data.banner(svc).title == "All monitors are up"
    svc = [data.ServiceView("s", "up", [mv("a", "up", failing=1), mv("b", "up")])]
    assert data.banner(svc).level == "failing" and "not down yet" in data.banner(svc).detail
    assert data.banner([data.ServiceView("s", "unknown", [mv("a", "unknown")])]).level == "unknown"


# --------------------------------------------------------------------------- alert formats


def alert(n_failed=1, **kw):
    failed = [
        FailedMonitor(
            f"monitor-{i}",
            "reports",
            "fail",
            f"reason {i} {EVIL} " + "x" * 50,
            [{"path": "$.total", "severity": "breaking", "message": "integer → string"}],
            "2026-11-05T09:00:00+00:00",
        )
        for i in range(n_failed)
    ]
    defaults = dict(
        event="incident",
        project="shop",
        environment="prod",
        total=10,
        failed=failed,
        service="reports",
        dashboard_url="https://monitor.example.com",
        brand="Example Co",
        accent="#0e7490",
    )
    return Alert(**{**defaults, **kw})


def test_alert_text_has_since_and_link():
    a = alert()
    assert "(since " in a.text and a.text.endswith("Open: https://monitor.example.com/monitors/monitor-0")
    assert a.to_dict()["kind"] == "down" and a.link.endswith("/monitors/monitor-0")
    assert alert(n_failed=2).link == "https://monitor.example.com/"
    assert alert(n_failed=0, recovered=[RecoveredMonitor("m", "reports", "5 min")]).kind == "recovered"


def test_email_html_escapes_and_colors():
    html = formats.email_html(alert())
    assert EVIL not in html and "&lt;script&gt;" in html
    assert "#c92a2a" in html and "Open dashboard" in html and "since " in html
    assert 'href="https://monitor.example.com/monitors/monitor-0"' in html
    rec = formats.email_html(alert(n_failed=0, recovered=[RecoveredMonitor("m", "reports", "5 min")]))
    assert "#0f8a5f" in rec and "was down 5 min" in rec
    reminder = formats.email_html(alert(reminder=True))
    assert "#b25e09" in reminder and "Still down" in reminder


def test_embed_respects_limits():
    big = alert(n_failed=40)
    for f in big.failed:
        f.summary = "y" * 3000
    card = formats.embed(big)["embeds"][0]
    total = (
        len(card["title"])
        + len(card["description"])
        + len(card["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in card["fields"])
    )
    assert total <= 6000 and len(card["fields"]) <= 11
    assert all(len(f["value"]) <= 1024 for f in card["fields"])
    assert card["fields"][-1]["name"].startswith("+") and card["color"] == 0xC92A2A
    assert card["url"] == "https://monitor.example.com/"
    test_card = formats.embed(Alert("test", "shop", None, 0, []))["embeds"][0]
    assert test_card["description"] == "Notifications are working." and "url" not in test_card


def test_blocks_escape_and_button():
    payload = formats.blocks(alert(n_failed=30))
    kinds = [b["type"] for b in payload["blocks"]]
    assert kinds[0] == "header" and kinds[-1] == "actions" and kinds.count("section") == 20
    text = json.dumps(payload)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert payload["blocks"][-1]["elements"][0]["url"] == "https://monitor.example.com/"
    assert "+10 more" in payload["blocks"][-2]["elements"][0]["text"]
    assert len(payload["blocks"][0]["text"]["text"]) <= 150


@respx.mock
def test_webhook_formats_are_sent():
    route = respx.post("https://hooks.test/x").respond(204)
    for fmt, key in (("embed", "embeds"), ("blocks", "blocks"), ("json", "text")):
        asyncio.run(
            send_webhook(WebhookNotifier(type="webhook", url="https://hooks.test/x", format=fmt), alert())
        )
        assert key in json.loads(route.calls[-1].request.content)
    # a custom body still wins over the format
    cfg = WebhookNotifier(
        type="webhook", url="https://hooks.test/x", format="embed", body={"content": "{{ alert.subject }}"}
    )
    asyncio.run(send_webhook(cfg, alert()))
    assert json.loads(route.calls[-1].request.content) == {"content": alert().subject}


def test_session_cookie_name_unchanged():
    assert COOKIE == "api_qovrix_session"  # renaming it would log everyone out on upgrade
