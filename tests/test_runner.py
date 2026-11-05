import asyncio
import json

import httpx
import pytest
import respx

from api_qovrix.config.loader import load_project
from api_qovrix.engine.runner import RunOptions, run_monitors, select_monitors

BASE = """
version: 1
environments:
  test:
    base_url: https://api.test
secrets:
  TOKEN: ${env:QOVRIX_T_TOKEN}
  KEY: ${env:QOVRIX_T_KEY}
"""


def run(root, names=(), mode="check", **opts):
    project = load_project(root / "api-qovrix.yaml")
    monitors = select_monitors(project, names)
    options = RunOptions(baseline_mode=mode, retry_delay=0, **opts)
    return asyncio.run(run_monitors(project, monitors, options))


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("QOVRIX_T_TOKEN", "tok-123456")
    monkeypatch.setenv("QOVRIX_T_KEY", "key-abcdef")


@respx.mock
def test_single_check_pass_and_fail(make_project):
    respx.get("https://api.test/ok").respond(200, json={"status": "up"})
    respx.get("https://api.test/bad").respond(200, json={"status": "down"})
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: ok
            request: { url: /ok, auth: { type: bearer, token: "{{ secrets.TOKEN }}" } }
            assert: [{ status: 200 }, { jsonpath: $.status, equals: up }]
          - name: bad
            request: { url: /bad }
            assert: [{ jsonpath: $.status, equals: up }]
    """
        },
    )
    summary = run(root)
    by = {r.name: r for r in summary.results}
    assert by["ok"].status == "pass"
    assert by["bad"].status == "fail"
    assert by["bad"].failure_summary == "$.status equals 'up': expected 'up', got 'down'"
    assert respx.calls[0].request.headers["Authorization"] == "Bearer tok-123456"
    assert not summary.ok


@respx.mock
def test_status_400_without_status_assertion_fails(make_project):
    respx.get("https://api.test/x").respond(404)
    root = make_project(BASE, {"monitors/a.yaml": "monitors:\n  - name: x\n    request: { url: /x }\n"})
    assert run(root).results[0].status == "fail"


@respx.mock
def test_flow_extract_and_cleanup(make_project):
    respx.post("https://api.test/carts").respond(201, json={"id": "c1"})
    respx.post("https://api.test/carts/c1/items").respond(500)
    delete = respx.delete("https://api.test/carts/c1").respond(204)
    respx.get("https://api.test/never").respond(200)
    root = make_project(
        BASE,
        {
            "monitors/flow.yaml": """
        monitors:
          - name: checkout
            steps:
              - name: create-cart
                request: { method: POST, url: /carts, json: { a: 1 } }
                extract: { cart_id: $.id }
                assert: [{ status: 201 }]
              - name: add-item
                request: { method: POST, url: "/carts/{{ steps.create-cart.cart_id }}/items" }
                assert: [{ status: 200 }]
              - name: skipped
                request: { url: /never }
              - name: cleanup
                always_run: true
                request: { method: DELETE, url: "/carts/{{ steps.create-cart.cart_id }}" }
                assert: [{ status: 204 }]
    """
        },
    )
    result = run(root).results[0]
    assert result.status == "fail"
    assert [s.skipped for s in result.steps] == [False, False, True, False]
    assert delete.called
    assert result.failure_summary.startswith("add-item: status 200")


@respx.mock
def test_retries(make_project):
    route = respx.get("https://api.test/flaky")
    route.side_effect = [httpx.Response(503), httpx.Response(200)]
    root = make_project(
        BASE, {"monitors/a.yaml": "monitors:\n  - name: f\n    retries: 2\n    request: { url: /flaky }\n"}
    )
    result = run(root).results[0]
    assert result.status == "pass" and result.attempts == 2


@respx.mock
def test_connection_error_is_error_status(make_project):
    respx.get("https://api.test/down").mock(side_effect=httpx.ConnectError("refused"))
    root = make_project(BASE, {"monitors/a.yaml": "monitors:\n  - name: d\n    request: { url: /down }\n"})
    result = run(root).results[0]
    assert result.status == "error"
    assert "connection failed" in result.failure_summary


@respx.mock
def test_oauth2_token_cached_and_api_key_query_redacted(make_project):
    token = respx.post("https://auth.test/token").respond(
        200, json={"access_token": "at-999999", "expires_in": 3600}
    )
    api = respx.get(url__startswith="https://api.test/items").respond(200, json=[])
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        service:
          auth:
            type: oauth2_client_credentials
            token_url: https://auth.test/token
            client_id: cid
            client_secret: "{{ secrets.KEY }}"
        monitors:
          - name: one
            request: { url: /items }
          - name: two
            request: { url: /items, params: { page: 2 } }
          - name: keyed
            auth: { type: api_key, name: api_key, in: query, value: "{{ secrets.KEY }}" }
            request: { url: /items }
    """
        },
    )
    summary = run(root, concurrency=1)
    assert summary.ok
    assert token.call_count == 1
    form = dict(x.split("=") for x in token.calls[0].request.content.decode().split("&"))
    assert form["grant_type"] == "client_credentials"
    assert token.calls[0].request.headers["Authorization"].startswith("Basic ")
    assert api.calls[0].request.headers["Authorization"] == "Bearer at-999999"
    keyed = next(r for r in summary.results if r.name == "keyed")
    assert "key-abcdef" not in keyed.steps[0].url and "***" in keyed.steps[0].url


@respx.mock
def test_allowed_hosts(make_project):
    root = make_project(
        BASE + "allowed_hosts: ['*.example.com']\n",
        {"monitors/a.yaml": "monitors:\n  - name: x\n    request: { url: /x }\n"},
    )
    result = run(root).results[0]
    assert result.status == "error" and "allowed_hosts" in result.failure_summary


@respx.mock
def test_missing_secret_is_reported(make_project, monkeypatch):
    monkeypatch.delenv("QOVRIX_T_TOKEN")
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: x
            request: { url: /x, headers: { Authorization: "Bearer {{ secrets.TOKEN }}" } }
    """
        },
    )
    result = run(root).results[0]
    assert result.status == "error" and "QOVRIX_T_TOKEN" in result.failure_summary


@respx.mock
def test_drift_lifecycle(make_project):
    route = respx.get("https://api.test/order")
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: order
            request: { url: /order }
            drift: { enabled: true, ignore: ["$.request_id"] }
    """
        },
    )
    good = {"id": 1, "total": 10, "email": "a@b.c", "request_id": "r1"}

    route.respond(200, json=good)
    first = run(root).results[0]
    assert first.status == "pass" and first.steps[0].drift.status == "no_baseline"
    assert any("no baseline" in w for w in first.warnings)

    assert run(root, mode="update").results[0].steps[0].drift.status == "updated"
    baseline = json.loads((root / ".api-qovrix/baselines/order.json").read_text(encoding="utf-8"))
    assert baseline["steps"]["main"]["accepted"] is True
    assert "a@b.c" not in json.dumps(baseline)  # values of sensitive fields are not stored

    route.respond(200, json={**good, "request_id": 5, "extra": True})
    ok = run(root).results[0]
    assert ok.status == "pass" and {c.severity for c in ok.steps[0].drift.changes} == {"info"}

    route.respond(200, json={"id": 1, "total": "10", "email": "a@b.c", "request_id": "r"})
    broken = run(root).results[0]
    assert broken.status == "fail"
    assert broken.steps[0].drift.worst == "breaking"

    run(root, mode="replace")
    assert run(root).results[0].status == "pass"


@respx.mock
def test_learning_baseline_is_informational(make_project):
    respx.get("https://api.test/o").respond(200, json={"a": 1})
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": (
                "monitors:\n  - name: o\n    request: { url: /o }\n    drift: { min_samples: 3 }\n"
            )
        },
    )
    run(root, mode="learn")
    respx.get("https://api.test/o").respond(200, json={"a": "x"})
    result = run(root).results[0]
    assert result.status == "pass"
    assert result.steps[0].drift.status == "learning"
    assert result.steps[0].drift.changes[0].severity == "info"


@respx.mock
def test_disabled_monitor_is_skipped(make_project):
    root = make_project(
        BASE,
        {"monitors/a.yaml": "monitors:\n  - name: paused\n    enabled: false\n    request: { url: /x }\n"},
    )
    summary = run(root)
    assert summary.results[0].status == "skipped" and summary.ok


def test_select_by_service_and_tag(make_project):
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": (
                "monitors:\n  - name: a1\n    tags: [smoke]\n    request: { url: /x }\n"
                "  - name: a2\n    request: { url: /x }\n"
            ),
            "monitors/b.yaml": "monitors:\n  - name: b1\n    tags: [smoke]\n    request: { url: /x }\n",
        },
    )
    project = load_project(root / "api-qovrix.yaml")
    assert [m.name for m in select_monitors(project, services=["a"])] == ["a1", "a2"]
    assert [m.name for m in select_monitors(project, tags=["smoke"])] == ["a1", "b1"]
    with pytest.raises(Exception, match="unknown service"):
        select_monitors(project, services=["zzz"])


@respx.mock
def test_update_baselines_skips_failed_steps(make_project):
    respx.get("https://api.test/o").respond(200, json={"state": "broken"})
    root = make_project(
        BASE,
        {
            "monitors/a.yaml": """
        monitors:
          - name: o
            request: { url: /o }
            assert: [{ jsonpath: $.state, equals: ok }]
            drift: {}
    """
        },
    )
    result = run(root, mode="update").results[0]
    assert result.status == "fail"
    assert not (root / ".api-qovrix/baselines/o.json").exists()
    assert any("baseline not updated" in w for w in result.warnings)
