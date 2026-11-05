"""Cron schedules, heartbeat pings, auto-reload of monitor files, and /healthz."""

import asyncio
import os
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from api_qovrix.config.loader import load_project
from api_qovrix.engine.runner import RunOptions
from api_qovrix.server import scheduler as sched
from api_qovrix.server.cron import Cron, CronError, timezone
from api_qovrix.server.scheduler import Server, prepare_data_dir, watched_files
from api_qovrix.server.storage import Storage
from api_qovrix.web.app import create_app
from api_qovrix.web.auth import Auth

UTC_TZ = timezone("UTC")


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --------------------------------------------------------------------------- cron


@pytest.mark.parametrize(
    "expr,after,expected",
    [
        ("*/15 * * * *", at(2026, 11, 5, 10, 7), at(2026, 11, 5, 10, 15)),
        ("0 8 * * *", at(2026, 11, 5, 8, 0), at(2026, 11, 6, 8, 0)),  # strictly after
        ("0 8 * * mon-fri", at(2026, 11, 6, 9, 0), at(2026, 11, 9, 8, 0)),  # Fri 9:00 -> Mon 8:00
        ("30 2 1 * *", at(2026, 11, 5, 0, 0), at(2026, 12, 1, 2, 30)),
        ("0 0 29 feb *", at(2026, 3, 1, 0, 0), at(2028, 2, 29, 0, 0)),  # next leap day
        ("0 12 13 * fri", at(2026, 11, 5, 0, 0), at(2026, 11, 6, 12, 0)),  # day OR weekday
        ("@hourly", at(2026, 11, 5, 10, 59), at(2026, 11, 5, 11, 0)),
        ("0 9 * * 7", at(2026, 11, 5, 0, 0), at(2026, 11, 8, 9, 0)),  # 7 = Sunday
        ("5,35 */6 * * *", at(2026, 11, 5, 6, 6), at(2026, 11, 5, 6, 35)),
    ],
)
def test_cron_next(expr, after, expected):
    assert Cron.parse(expr).next_after(after, UTC_TZ) == expected


def test_cron_in_a_time_zone():
    manila = timezone("Asia/Manila")  # UTC+8, no daylight saving
    nxt = Cron.parse("0 8 * * *").next_after(at(2026, 11, 5, 0, 30), manila)  # 08:30 in Manila
    assert nxt.astimezone(UTC) == at(2026, 11, 6, 0, 0)  # next 8:00 Manila = 00:00 UTC tomorrow


@pytest.mark.parametrize(
    "expr,message",
    [
        ("* * * *", "needs 5 fields"),
        ("60 * * * *", "outside 0-59"),
        ("* 24 * * *", "outside 0-23"),
        ("* * * 13 *", "outside 1-12"),
        ("*/0 * * * *", "bad step"),
        ("5-1 * * * *", "outside"),
        ("x * * * *", "not a number"),
        ("1,,2 * * * *", "empty list item"),
    ],
)
def test_cron_errors(expr, message):
    with pytest.raises(CronError, match=message):
        Cron.parse(expr)


def test_cron_that_never_matches_and_bad_timezone():
    with pytest.raises(CronError, match="never matches"):
        Cron.parse("0 0 31 feb *").next_after(at(2026, 1, 1, 0, 0), UTC_TZ)
    with pytest.raises(CronError, match="unknown time zone"):
        timezone("Mars/Olympus")


# --------------------------------------------------------------------------- project helpers

CONFIG = """
version: 1
project: shop
environments: { prod: { base_url: "https://api.test" } }
defaults: { interval: 1m }
server:
  timezone: UTC
  heartbeat: { url: "https://beat.test/ping", interval: 0.1s }
"""


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "api-qovrix.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "monitors").mkdir()
    (tmp_path / "monitors/a.yaml").write_text(
        "monitors:\n  - name: one\n    request: { url: /one }\n  - name: two\n    request: { url: /two }\n",
        encoding="utf-8",
    )
    return tmp_path


def make_server(root, transport, logs):
    project = load_project(root / "api-qovrix.yaml")
    storage = Storage(prepare_data_dir(root / "data"))
    server = Server(
        project,
        storage,
        RunOptions(transport=transport, retry_delay=0),
        log=logs.append,
        reloader=lambda: load_project(root / "api-qovrix.yaml"),
    )
    return server, storage


def test_invalid_timezone_is_a_config_error(project_dir):
    from api_qovrix.errors import ConfigError

    (project_dir / "api-qovrix.yaml").write_text(CONFIG.replace("timezone: UTC", "timezone: Nowhere/X"))
    with pytest.raises(ConfigError, match=r"server\.timezone"):
        load_project(project_dir / "api-qovrix.yaml")


# --------------------------------------------------------------------------- scheduler


def test_cron_monitor_waits_for_its_time(project_dir, monkeypatch):
    """A cron monitor runs at its next matching minute, not right away like interval monitors."""
    (project_dir / "monitors/a.yaml").write_text(
        "monitors:\n  - name: daily\n    cron: '0 8 * * *'\n    request: { url: /one }\n", encoding="utf-8"
    )
    logs: list[str] = []
    calls: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.test":  # ignore heartbeat pings
            calls.append(request.url.path)
        return handler(request)

    server, storage = make_server(project_dir, httpx.MockTransport(counting), logs)
    server.clock = lambda: at(2026, 11, 5, 7, 59, 59)  # one second before 08:00
    sleeps: list[float] = []
    real_sleep = server._sleep

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(min(seconds, 0.05))

    server._sleep = recording_sleep  # type: ignore[method-assign]

    async def go():
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.3)
        server.stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    storage.close()
    assert 1.0 in sleeps  # waited exactly until 08:00:00
    assert calls and calls[0] == "/one"
    assert "cron in UTC" in logs[0]


def test_heartbeat_pings_while_healthy_and_never_logs_the_url(project_dir, monkeypatch):
    monkeypatch.setattr(sched, "FIRST_RUN_SPREAD", 0.0)
    pings: list[str] = []
    fail = {"on": False}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "beat.test":
            pings.append(str(request.url))
            return httpx.Response(500 if fail["on"] else 200)
        return handler(request)

    logs: list[str] = []
    server, storage = make_server(project_dir, httpx.MockTransport(route), logs)

    async def go():
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.35)
        fail["on"] = True
        await asyncio.sleep(0.25)
        fail["on"] = False
        await asyncio.sleep(0.25)
        server.stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    storage.close()
    assert len(pings) >= 4 and pings[0] == "https://beat.test/ping"
    assert any(line == "! heartbeat failed: HTTP 500" for line in logs)
    assert "heartbeat ok again" in logs
    assert not any("beat.test" in line for line in logs)


def test_reload_restarts_only_changed_monitors_and_survives_bad_edits(project_dir, monkeypatch):
    monkeypatch.setattr(sched, "FIRST_RUN_SPREAD", 0.0)
    monkeypatch.setattr(sched, "WATCH_EVERY", 0.05)
    logs: list[str] = []
    server, storage = make_server(project_dir, httpx.MockTransport(handler), logs)
    monitors = project_dir / "monitors/a.yaml"

    def write(text: str) -> None:
        monitors.write_text(text, encoding="utf-8")
        os.utime(monitors, (time.time() + 5, time.time() + 5))  # make sure the mtime changes

    async def go():
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.2)
        first_one = server._loops["one"]
        first_two = server._loops["two"]

        write(
            "monitors:\n  - name: one\n    request: { url: /one }\n"
            "  - name: two\n    request: { url: /two-changed }\n"
            "  - name: three\n    request: { url: /three }\n"
        )
        await asyncio.sleep(0.4)
        assert server._loops["one"] is first_one  # unchanged: not restarted
        assert server._loops["two"] is not first_two and first_two.cancelled()
        assert "three" in server._loops
        assert {m.name for m in server.project.monitors} == {"one", "two", "three"}

        write("monitors: [ this is not valid")
        await asyncio.sleep(0.4)
        assert {m.name for m in server.project.monitors} == {"one", "two", "three"}  # kept

        write("monitors:\n  - name: one\n    request: { url: /one }\n")
        await asyncio.sleep(0.4)
        assert set(server._loops) == {"one"}
        server.stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    states = {s.monitor for s in asyncio.run(storage.all_states())}
    storage.close()
    assert "reloaded monitors (+1 added, -0 removed, ~1 changed)" in logs
    assert any(line.startswith("! reload failed, keeping the previous monitors") for line in logs)
    assert "reloaded monitors (+0 added, -2 removed, ~0 changed)" in logs
    assert states == {"one"}


def test_watched_files_include_config_monitors_and_spec(project_dir):
    (project_dir / "openapi.json").write_text('{"openapi": "3.0.0", "info": {"title": "x", "version": "1"}}')
    text = (project_dir / "api-qovrix.yaml").read_text(encoding="utf-8")
    (project_dir / "api-qovrix.yaml").write_text(
        text + "openapi: { source: openapi.json }\n", encoding="utf-8"
    )
    files = watched_files(load_project(project_dir / "api-qovrix.yaml"))
    names = {os.path.basename(p) for p in files}
    assert names == {"api-qovrix.yaml", "a.yaml", "openapi.json"}


def test_healthz_reports_scheduler_state(project_dir, monkeypatch):
    monkeypatch.setattr(sched, "FIRST_RUN_SPREAD", 0.0)
    logs: list[str] = []
    server, storage = make_server(project_dir, httpx.MockTransport(handler), logs)
    with TestClient(create_app(server, Auth(None, None))) as c:
        assert c.get("/healthz").json()["status"] == "ok"  # dashboard without a scheduler (selftest)
        server.started_at = "2026-11-05T00:00:00+00:00"
        body = c.get("/healthz").json()
        assert body["status"] == "ok" and body["monitors"] == 2
        server.stop()
        r = c.get("/healthz")
        assert r.status_code == 503 and r.json()["status"] == "unavailable"
    storage.close()


def test_dashboard_shows_cron_schedule(project_dir):
    (project_dir / "monitors/a.yaml").write_text(
        "monitors:\n  - name: daily\n    cron: '0 8 * * mon-fri'\n    request: { url: /one }\n",
        encoding="utf-8",
    )
    server, storage = make_server(project_dir, httpx.MockTransport(handler), [])
    with TestClient(create_app(server, Auth(None, None))) as c:
        assert "Cron 0 8 * * mon-fri" in c.get("/monitors/daily").text
    storage.close()
