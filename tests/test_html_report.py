import os
import re

import respx
from typer.testing import CliRunner

from api_qovrix.cli.app import app
from api_qovrix.engine.results import (
    AssertionResult,
    DriftChange,
    DriftReport,
    MonitorResult,
    RunSummary,
    StepResult,
)
from api_qovrix.report.html import render_html

EVIL = "<script>alert(1)</script>"


def summary():
    bad = StepResult(
        "main", "GET", "https://api.test/o", status_code=200, duration_ms=12.0, timings={"total": 12.0}
    )
    bad.assertions = [AssertionResult("$.name equals 'x'", False, f"expected 'x', got '{EVIL}'")]
    bad.drift = DriftReport(
        "changed", changes=[DriftChange("$.total", "breaking", "integer → string")], failed=True
    )
    ok = StepResult("main", "GET", "https://api.test/ok", status_code=200, duration_ms=5.0)
    err = StepResult("main", "GET", "https://api.test/down", error="connection failed: refused")
    results = [
        MonitorResult("orders", "shop", "pass", steps=[ok], duration_ms=5.0),
        MonitorResult(
            "evil", "shop", "fail", steps=[bad], duration_ms=12.0, warnings=["main: no baseline yet"]
        ),
        MonitorResult("down", "billing", "error", steps=[err], duration_ms=1.0, attempts=2),
        MonitorResult("paused", "billing", "skipped"),
    ]
    return RunSummary(results, "prod", 1234.0)


def test_report_content_and_order(monkeypatch):
    monkeypatch.setenv("API_QOVRIX_RUN_URL", "https://ci.example.com/run/9")
    html = render_html(summary(), "shop-api")
    assert "<title>shop-api · run report</title>" in html
    assert "✘ shop-api · run report" in html and "env prod" in html and "1.2 s" in html
    assert 'href="https://ci.example.com/run/9"' in html
    counts = re.findall(r"<b>(\d+)</b><span>(\w+)</span>", html)
    assert counts == [("1", "failed"), ("1", "errors"), ("1", "passed"), ("1", "skipped")]
    # services with problems first, failing monitors first and expanded
    assert html.index("<h2>billing</h2>") < html.index("<h2>shop</h2>")
    assert html.index(">evil<") < html.index(">orders<")
    assert html.count("<details open>") == 2
    assert "$.total" in html and "integer → string" in html and "! main: no baseline yet" in html
    assert "connection failed: refused" in html and "attempt 2" in html
    assert "font-family" not in html or "system-ui" in html  # stylesheet inlined unescaped


def test_report_escapes_response_values_and_is_self_contained():
    html = render_html(summary())
    assert EVIL not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script" not in html
    assert not re.search(r'<link[^>]+href="https?://', html)
    assert "default-src 'none'" in html  # blocks anything that slips in


@respx.mock
def test_cli_html_option(tmp_path):
    respx.get("https://jsonplaceholder.typicode.com/todos/1").respond(200, json={"id": 1, "completed": False})
    respx.post("https://jsonplaceholder.typicode.com/posts").respond(201, json={"id": 101})
    respx.get("https://jsonplaceholder.typicode.com/posts/1/comments").respond(200, json=[{"email": "a@b.c"}])
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        CliRunner().invoke(app, ["init"])
        r = CliRunner().invoke(app, ["run", "--html", "reports/run.html"])
    finally:
        os.chdir(old)
    assert r.exit_code == 0, r.output
    html = (tmp_path / "reports/run.html").read_text()
    assert "example-todo" in html and "example-post-flow" in html and "✔" in html


def test_dashboard_escapes_response_values(tmp_path):
    import asyncio

    import httpx
    from fastapi.testclient import TestClient

    from api_qovrix.config.loader import load_project
    from api_qovrix.engine.runner import RunOptions
    from api_qovrix.server.scheduler import Server, prepare_data_dir
    from api_qovrix.server.storage import Storage
    from api_qovrix.web.app import create_app
    from api_qovrix.web.auth import Auth

    (tmp_path / "api-qovrix.yaml").write_text(
        "version: 1\nenvironments: { p: { base_url: 'https://api.test' } }\n"
    )
    (tmp_path / "monitors").mkdir()
    (tmp_path / "monitors/a.yaml").write_text(
        "monitors:\n  - name: evil\n    request: { url: /x }\n    assert: [{ jsonpath: $.name, equals: x }]\n"
    )
    project = load_project(tmp_path / "api-qovrix.yaml")
    storage = Storage(prepare_data_dir(tmp_path / "data"))
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"name": EVIL}))
    srv = Server(project, storage, RunOptions(transport=transport), log=lambda s: None)
    asyncio.run(srv.run_once())
    with TestClient(create_app(srv, Auth(None, None))) as c:
        for path in ("/", "/monitors/evil", "/incidents"):
            body = c.get(path).text
            assert EVIL not in body, path
        assert "&lt;script&gt;" in c.get("/monitors/evil").text
    storage.close()
