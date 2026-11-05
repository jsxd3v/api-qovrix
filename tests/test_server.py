import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import respx
from typer.testing import CliRunner

from api_qovrix.alerts.message import Alert, FailedMonitor, RecoveredMonitor, human_duration
from api_qovrix.cli.app import app
from api_qovrix.config.loader import load_project
from api_qovrix.engine.results import MonitorResult, StepResult
from api_qovrix.engine.runner import RunOptions
from api_qovrix.errors import ConfigError
from api_qovrix.server import scheduler as sched
from api_qovrix.server.alerting import AlertManager, next_state
from api_qovrix.server.scheduler import Server, prepare_data_dir
from api_qovrix.server.storage import MonitorState, Storage

HOOK = "https://hooks.test/x"
T0 = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)

CONFIG = """
version: 1
project: shop
environments: { prod: { base_url: "https://api.test" } }
notifiers:
  chat: { type: webhook, url: "https://hooks.test/x" }
defaults: { notify: [chat], interval: 1s, alert_after: 2 }
server: { group_wait: 0.05s }
"""
MONITORS = """
monitors:
  - name: orders
    request: { url: /orders }
  - name: users
    request: { url: /users }
  - name: paused-one
    enabled: false
    request: { url: /p }
"""


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def result(name="orders", service="shop", status="fail"):
    step = StepResult("main", "GET", "https://api.test/x", status_code=500 if status != "pass" else 200)
    return MonitorResult(name, service, status, steps=[step], duration_ms=12.0)


@pytest.fixture
def project(make_project):
    root = make_project(CONFIG, {"monitors/shop.yaml": MONITORS})
    return load_project(root / "api-qovrix.yaml")


@pytest.fixture
def storage(tmp_path):
    s = Storage(prepare_data_dir(tmp_path / "data"))
    yield s
    s.close()


# --------------------------------------------------------------------------- rules


def test_state_rules(project):
    m = project.monitor("orders")
    s = MonitorState("orders", "shop")

    s, t = next_state(s, m, result(status="fail"), T0)
    assert (s.state, s.consecutive_failures, t) == ("unknown", 1, None)

    s, t = next_state(s, m, result(status="fail"), T0 + timedelta(minutes=1))
    assert s.state == "down" and t.kind == "down" and s.since == (T0 + timedelta(minutes=1)).isoformat()

    s, t = next_state(s, m, result(status="error"), T0 + timedelta(minutes=2))
    assert s.state == "down" and t is None  # no repeat configured

    s, t = next_state(s, m, result(status="pass"), T0 + timedelta(minutes=13))
    assert s.state == "up" and t.kind == "recovered" and t.down_for == 12 * 60
    assert s.consecutive_failures == 0 and s.last_status == "pass"

    s, t = next_state(s, m, result(status="fail"), T0 + timedelta(minutes=14))
    assert s.state == "up" and s.consecutive_failures == 1 and t is None  # one failure isn't "down"

    s, t = next_state(s, m, result(status="skipped"), T0)
    assert s.state == "paused" and t is None
    s, t = next_state(s, m, result(status="fail"), T0)
    assert s.state == "unknown"


def test_reminders(project):
    m = project.monitor("orders")
    m.alert_repeat = 3600
    s = MonitorState("orders", "shop", state="down", consecutive_failures=5, since=T0.isoformat())
    s.last_alert_at = T0.isoformat()
    _, t = next_state(s, m, result(), T0 + timedelta(minutes=30))
    assert t is None
    _, t = next_state(s, m, result(), T0 + timedelta(hours=1))
    assert t.kind == "reminder"


def test_messages_and_durations():
    assert [human_duration(x) for x in (5, 89, 600, 3600, 5400, 90000, 200000)] == [
        "5s", "89s", "10 min", "60 min", "1h 30m", "25h", "2d 7h",
    ]  # fmt: skip
    down = Alert(
        "incident",
        "shop",
        "prod",
        3,
        [FailedMonitor("orders", "shop", "fail", "status 200: got 500")],
        service="shop",
    )
    assert down.subject == "shop: 1 monitor down · shop · prod"
    assert down.text.startswith("🔴 api-qovrix: 1 monitor down in shop (shop · prod)")
    both = Alert(
        "incident",
        None,
        None,
        3,
        down.failed,
        service="shop",
        recovered=[RecoveredMonitor("users", "shop", "4 min")],
    )
    assert both.subject == "shop: 1 monitor down, 1 recovered"
    assert "✅ Recovered:\n• users (was down 4 min)" in both.text
    rec = Alert("incident", None, None, 3, [], service="shop", recovered=both.recovered)
    assert rec.text.startswith("✅ api-qovrix: 1 monitor recovered in shop")
    rem = Alert("incident", None, None, 3, down.failed, service="shop", reminder=True)
    assert "still down" in rem.subject and "still down" in rem.text


# --------------------------------------------------------------------------- storage


def test_storage(storage, tmp_path):
    assert (tmp_path / "data" / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")

    async def go():
        old = (T0 - timedelta(days=40)).isoformat()
        r1 = await storage.record_run(result(status="fail"), "prod", old)
        r2 = await storage.record_run(result(status="pass"), "prod", T0.isoformat())
        rows = await storage.history("orders")
        assert [r["id"] for r in rows] == [r2, r1]
        details = await storage.run_details(r1)
        assert details["status"] == "fail" and details["steps"][0]["status_code"] == 500
        assert await storage.prune((T0 - timedelta(days=30)).isoformat()) == 1
        assert len(await storage.history("orders")) == 1

        await storage.save_state(MonitorState("orders", "shop", state="down"))
        await storage.save_state(MonitorState("gone", "shop", state="down"))
        await storage.open_incident("gone", "shop", T0.isoformat(), "x")
        await storage.open_incident("orders", "shop", T0.isoformat(), "y")
        assert len(await storage.incidents(open_only=True)) == 2
        assert await storage.forget_monitors({"orders"}) == 1
        assert [s.monitor for s in await storage.all_states()] == ["orders"]
        await storage.resolve_incident("orders", T0.isoformat())
        assert await storage.incidents(open_only=True) == []
        assert len(await storage.incidents()) == 2

    asyncio.run(go())


def test_storage_refuses_newer_schema(tmp_path):
    path = prepare_data_dir(tmp_path / "d")
    Storage(path).close()
    import sqlite3

    c = sqlite3.connect(path)
    c.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="newer api-qovrix"):
        Storage(path)


# --------------------------------------------------------------------------- grouping


@respx.mock
def test_alerts_grouped_per_service_and_notifier(project, storage):
    hook = respx.post(HOOK).respond(200)
    clock = Clock()
    logs = []
    mgr = AlertManager(project, storage, clock, log=logs.append)

    async def go():
        for _ in range(2):
            await mgr.process(project.monitor("orders"), result("orders"))
            await mgr.process(project.monitor("users"), result("users"))
        assert [t.kind for t in mgr.pending] == ["down", "down"]
        sent = await mgr.flush()
        assert len(sent) == 1 and sent[0].ok
        body = json.loads(hook.calls[0].request.content)
        assert body["event"] == "incident" and body["service"] == "shop"
        assert [m["name"] for m in body["failed"]] == ["orders", "users"]
        state = await storage.get_state("orders")
        assert state.last_alert_at == T0.isoformat()
        assert len(await storage.incidents(open_only=True)) == 2

        clock.advance(minutes=5)
        await mgr.process(project.monitor("orders"), result("orders", status="pass"))
        await mgr.flush()
        body = json.loads(hook.calls[1].request.content)
        assert body["recovered"] == [{"name": "orders", "service": "shop", "down_for": "5 min"}]
        assert body["failed"] == []
        assert [i.monitor for i in await storage.incidents(open_only=True)] == ["users"]
        assert await mgr.flush() == []  # nothing pending

    asyncio.run(go())
    assert any("alert sent → chat: shop: 2 monitors down" in line for line in logs)


@respx.mock
def test_failed_delivery_is_logged(project, storage):
    respx.post(HOOK).respond(500)
    logs = []
    mgr = AlertManager(project, storage, Clock(), log=logs.append)

    async def go():
        for _ in range(2):
            await mgr.process(project.monitor("orders"), result())
        await mgr.flush()

    asyncio.run(go())
    assert any("alert failed → chat: webhook returned HTTP 500" in line for line in logs)


def test_monitors_without_notifiers_keep_state_but_dont_queue(project, storage):
    m = project.monitor("orders")
    m.notify = []
    mgr = AlertManager(project, storage, Clock(), log=lambda s: None)

    async def go():
        for _ in range(2):
            await mgr.process(m, result())
        assert mgr.pending == []
        assert (await storage.get_state("orders")).state == "down"

    asyncio.run(go())


# --------------------------------------------------------------------------- server


@respx.mock
def test_run_once_flow(project, storage):
    respx.get("https://api.test/orders").respond(500)
    respx.get("https://api.test/users").respond(200)
    hook = respx.post(HOOK).respond(200)
    logs = []
    server = Server(project, storage, RunOptions(retry_delay=0), clock=Clock(), log=logs.append)

    async def go():
        await server.run_once()
        assert not hook.called
        await server.run_once()
        assert hook.call_count == 1
        states = {s.monitor: s.state for s in await storage.all_states()}
        assert states == {"orders": "down", "users": "up", "paused-one": "paused"}
        assert len(await storage.history("orders")) == 2
        await server.ctx.clients.aclose()

    asyncio.run(go())
    assert any(line.startswith("✘ orders [shop] 500") for line in logs)


def test_cron_is_rejected(make_project, storage):
    root = make_project(
        CONFIG,
        {"monitors/a.yaml": "monitors:\n  - name: c\n    cron: '* * * * *'\n    request: { url: /c }\n"},
    )
    with pytest.raises(ConfigError, match="cron"):
        Server(load_project(root / "api-qovrix.yaml"), storage)


@respx.mock
def test_serve_loop_runs_on_interval_and_stops(project, storage, monkeypatch):
    monkeypatch.setattr(sched, "FIRST_RUN_SPREAD", 0.0)
    for m in project.monitors:
        m.interval = 0.1
    respx.get("https://api.test/orders").respond(500)
    respx.get("https://api.test/users").respond(200)
    hook = respx.post(HOOK).respond(200)
    logs = []
    server = Server(project, storage, RunOptions(retry_delay=0), log=logs.append)

    async def go():
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.6)
        server.stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert len(asyncio.run(storage.history("orders"))) >= 3
    assert hook.call_count == 1  # down once, no spam while still down
    assert logs[0].startswith("api-qovrix serve: 2 monitors") and logs[-1] == "stopped"


# --------------------------------------------------------------------------- CLI


def cli(root, *args):
    old = os.getcwd()
    os.chdir(root)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(old)


@respx.mock
def test_cli_serve_once_services_history(make_project):
    respx.get("https://api.test/orders").respond(500)
    respx.get("https://api.test/users").respond(200)
    respx.post(HOOK).respond(200)
    root = make_project(CONFIG, {"monitors/shop.yaml": MONITORS})

    r = cli(root, "services")
    assert r.exit_code == 0 and "No history yet" in r.output
    r = cli(root, "serve", "--once")
    assert r.exit_code == 0, r.output
    assert (root / ".api-qovrix/data/api-qovrix.db").is_file()
    r = cli(root, "services")
    assert "orders" in r.output and "failing 1/2" in r.output and "paused" in r.output
    cli(root, "serve", "--once", "--log-format", "json")
    r = cli(root, "services")
    assert "● shop down" in r.output
    r = cli(root, "history", "orders")
    assert r.exit_code == 0 and r.output.count("fail") >= 2
    assert cli(root, "history", "ghost").exit_code == 2
    assert cli(root, "serve", "--log-format", "xml").exit_code == 2

    custom = root / "elsewhere"
    r = cli(root, "serve", "--once", "--data-dir", str(custom))
    assert r.exit_code == 0 and (custom / "api-qovrix.db").is_file()
