"""Error paths: auth failures, timeouts, failure reports and the drift CLI commands."""

import asyncio
import json
import os

import httpx
import respx
from rich.console import Console
from typer.testing import CliRunner

from api_qovrix.cli.app import app
from api_qovrix.config.loader import load_project
from api_qovrix.engine.runner import RunOptions, run_monitors, select_monitors
from api_qovrix.report.output import print_summary, to_junit

BASE = """
version: 1
environments: { test: { base_url: "https://api.test" } }
secrets:
  CID: ${env:QOVRIX_E_CID}
  SECRET: ${env:QOVRIX_E_SECRET}
"""


def run(root):
    project = load_project(root / "api-qovrix.yaml")
    return asyncio.run(run_monitors(project, select_monitors(project), RunOptions(retry_delay=0)))


def setup_env(monkeypatch):
    monkeypatch.setenv("QOVRIX_E_CID", "client-1")
    monkeypatch.setenv("QOVRIX_E_SECRET", "s3cret-value")


@respx.mock
def test_auth_variants_and_token_errors(make_project, monkeypatch):
    setup_env(monkeypatch)
    token = respx.post("https://auth.test/token")
    token.side_effect = [
        httpx.Response(200, json={"access_token": "tok-aaaaaa", "expires_in": "60"}),
        httpx.Response(401),
        httpx.Response(200, json={"nope": 1}),
    ]
    api = respx.get(url__startswith="https://api.test/").respond(200, json={})
    oauth = """
              type: oauth2_client_credentials
              token_url: https://auth.test/token
              client_id: "{{ secrets.CID }}"
              client_secret: "{{ secrets.SECRET }}"
    """.strip("\n")
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": f"""
        monitors:
          - name: basic
            auth: {{ type: basic, username: u, password: "{{{{ secrets.SECRET }}}}" }}
            request: {{ url: /b }}
          - name: header-key
            auth: {{ type: api_key, name: X-Key, value: "{{{{ secrets.SECRET }}}}" }}
            request: {{ url: /k }}
          - name: oauth-post
            auth:
              client_auth: post
              scope: read
              audience: aud
{oauth}
            request: {{ url: /o1 }}
          - name: oauth-401
            auth:
              scope: other
{oauth}
            request: {{ url: /o2 }}
          - name: oauth-bad
            auth:
              scope: third
{oauth}
            request: {{ url: /o3 }}
    """
        },
    )
    project = load_project(root / "api-qovrix.yaml")
    summary = asyncio.run(
        run_monitors(project, select_monitors(project), RunOptions(retry_delay=0, concurrency=1))
    )
    by = {r.name: r for r in summary.results}
    assert by["basic"].status == "pass" and by["header-key"].status == "pass"
    headers = {c.request.url.path: c.request.headers for c in api.calls}
    assert headers["/b"]["Authorization"].startswith("Basic ")
    assert headers["/k"]["X-Key"] == "s3cret-value"
    assert by["oauth-post"].status == "pass"
    body = token.calls[0].request.content.decode()
    assert "client_secret=s3cret-value" in body and "scope=read" in body and "audience=aud" in body
    assert "returned 401" in by["oauth-401"].failure_summary
    assert "no access_token" in by["oauth-bad"].failure_summary


@respx.mock
def test_timeout_and_bad_regex_and_relative_url(make_project, monkeypatch):
    setup_env(monkeypatch)
    respx.get("https://api.test/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://api.test/re").respond(200, json={"a": "b"})
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: slow
            timeout: 1s
            request: { url: /slow }
          - name: bad-regex
            request: { url: /re }
            assert: [{ jsonpath: $.a, matches: "(" }]
          - name: no-base
            base_url: ""
            request: { url: relative/path }
    """
        },
    )
    by = {r.name: r for r in run(root).results}
    assert by["slow"].status == "error" and "timed out after 1s" in by["slow"].failure_summary
    assert by["bad-regex"].status == "error" and "invalid regex" in by["bad-regex"].failure_summary
    assert by["no-base"].status == "error" and "no base_url" in by["no-base"].failure_summary


@respx.mock
def test_failure_reports(make_project, monkeypatch):
    setup_env(monkeypatch)
    respx.get("https://api.test/a").respond(500)
    respx.get("https://api.test/b").respond(200, json={"x": 1})
    respx.get("https://api.test/c").mock(side_effect=httpx.ConnectError("down"))
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: five-hundred
            request: { url: /a }
          - name: ok
            request: { url: /b }
            assert: [{ jsonpath: $.x, equals: 1 }]
          - name: down
            request: { url: /c }
          - name: paused
            enabled: false
            request: { url: /a }
    """
        },
    )
    summary = run(root)
    console = Console(record=True, width=200)
    print_summary(console, summary, verbose=True)
    text = console.export_text()
    assert "1 passed, 1 failed, 1 errors, 1 skipped" in text
    assert "status < 400: got 500" in text and "disabled" in text
    junit = to_junit(summary)
    assert "<failure" in junit and "<error" in junit and "<skipped" in junit


@respx.mock
def test_drift_cli_commands(make_project, monkeypatch):
    setup_env(monkeypatch)
    route = respx.get("https://api.test/item")
    route.respond(200, json={"state": "ok", "n": 1})
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: item
            request: { url: /item }
            drift: {}
          - name: writer
            request: { method: POST, url: /item }
            drift: {}
    """
        },
    )
    runner = CliRunner()
    old = os.getcwd()
    os.chdir(root)
    try:
        r = runner.invoke(app, ["drift", "show", "item"])
        assert "No baseline" in r.output
        r = runner.invoke(app, ["drift", "learn", "--samples", "2", "--delay", "0"])
        assert r.exit_code == 0, r.output
        assert "skipping monitors with non-GET steps" in r.output and "round 2/2" in r.output
        data = json.loads((root / ".api-qovrix/baselines/item.json").read_text(encoding="utf-8"))
        assert data["steps"]["main"]["samples"] == 2 and data["steps"]["main"]["accepted"] is False

        route.respond(200, json={"state": "ok"})
        r = runner.invoke(app, ["drift", "show", "item", "--live"])
        assert r.exit_code == 0 and "DRIFT (learning)" in r.output

        r = runner.invoke(app, ["drift", "accept", "item"])
        assert r.exit_code == 0, r.output
        data = json.loads((root / ".api-qovrix/baselines/item.json").read_text(encoding="utf-8"))
        main = data["steps"]["main"]
        assert main["accepted"] is True and "n" not in main["shape"]["props"]

        r = runner.invoke(app, ["run", "item", "--service", "a", "--tag", "none"])
        assert r.exit_code == 1 and "no monitors selected" in r.output
        assert runner.invoke(app, ["schema"]).exit_code == 0
    finally:
        os.chdir(old)


def test_accepted_changes_are_not_shown_as_errors():
    from api_qovrix.engine.results import DriftChange, DriftReport, MonitorResult, RunSummary, StepResult

    step = StepResult("main", "GET", "https://x", status_code=200)
    step.drift = DriftReport(
        "updated", changes=[DriftChange("$.a", "breaking", "field removed (was integer)")]
    )
    summary = RunSummary([MonitorResult("m", "s", "pass", steps=[step])], None, 1.0)
    console = Console(record=True, width=200)
    print_summary(console, summary)
    text = console.export_text()
    assert "BASELINE UPDATED (changes accepted)" in text
    assert "~ $.a" in text and "✘ $.a" not in text and "breaking" in text
