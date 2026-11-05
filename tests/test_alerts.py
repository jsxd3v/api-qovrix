import asyncio
import json
import os
import smtplib

import httpx
import pytest
import respx
from typer.testing import CliRunner

from api_qovrix.alerts.message import Alert, FailedMonitor, ci_run_url, make_test_alert
from api_qovrix.alerts.notifiers import parse_smtp_url, plan_run_alerts, send, send_email, send_webhook
from api_qovrix.cli.app import app
from api_qovrix.config.loader import load_project
from api_qovrix.config.models import EmailNotifier, WebhookNotifier
from api_qovrix.engine.results import DriftChange, DriftReport, MonitorResult, StepResult
from api_qovrix.errors import ConfigError

HOOK = "https://hooks.test/abc"


def failed_result(name="get-order", service="shop", drift=True):
    step = StepResult("main", "GET", "https://api.test/o", status_code=200)
    if drift:
        step.drift = DriftReport(
            "changed",
            changes=[
                DriftChange("$.total", "breaking", "integer → string"),
                DriftChange("$.extra", "info", "new field (string)"),
            ],
            failed=True,
        )
    return MonitorResult(name, service, "fail", steps=[step])


def alert(**kw):
    defaults = dict(
        event="run.failed",
        project="shop-api",
        environment="production",
        total=5,
        failed=[FailedMonitor.from_result(failed_result())],
    )
    return Alert(**{**defaults, **kw})


# --------------------------------------------------------------------------- message


def test_alert_text_and_subject():
    a = alert(run_url="https://ci/run/1")
    assert a.subject == "1 monitor failed · shop-api · production"
    assert a.text.splitlines()[0] == "❌ api-qovrix: 1 of 5 monitors failed (shop-api · production)"
    assert "• get-order [shop] fail: drift (breaking)" in a.text
    assert "$.total  integer → string (breaking)" in a.text
    assert "$.extra" not in a.text  # info changes are not alert-worthy
    assert a.text.endswith("Run: https://ci/run/1")
    data = a.to_dict()
    assert data["failed_count"] == 1 and data["text"] == a.text and data["failed"][0]["name"] == "get-order"


def test_test_alert_and_run_url(monkeypatch):
    for k in ("API_QOVRIX_RUN_URL", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    assert ci_run_url() is None
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert ci_run_url() == "https://github.com/acme/app/actions/runs/42"
    monkeypatch.setenv("API_QOVRIX_RUN_URL", "https://ci.example.com/job/7")
    t = make_test_alert("shop-api", None)
    assert t.subject == "Test alert" and "Notifications are working" in t.text
    assert t.run_url == "https://ci.example.com/job/7"


def test_routing_one_alert_per_notifier():
    results = [
        failed_result("a", "x"),
        failed_result("b", "y"),
        failed_result("lonely", "z"),
        MonitorResult("ok", "x", "pass"),
    ]
    notify_map = {"a": ["chat", "mail"], "b": ["chat"], "ok": ["chat"]}
    plan, unrouted = plan_run_alerts("p", "prod", results, notify_map)
    assert [n for n, _ in plan] == ["chat", "mail"]
    by = dict(plan)
    assert [m.name for m in by["chat"].failed] == ["a", "b"]
    assert [m.name for m in by["mail"].failed] == ["a"]
    assert by["chat"].total == 4
    assert unrouted == ["lonely"]
    assert plan_run_alerts("p", None, [MonitorResult("ok", "x", "pass")], {"ok": ["chat"]}) == ([], [])


# --------------------------------------------------------------------------- webhook


@respx.mock
def test_webhook_default_body():
    route = respx.post(HOOK).respond(200)
    asyncio.run(send_webhook(WebhookNotifier(type="webhook", url=HOOK, headers={"X-Token": "t"}), alert()))
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["text"].startswith("❌ api-qovrix") and body["event"] == "run.failed"
    assert req.headers["X-Token"] == "t" and req.headers["User-Agent"].startswith("api-qovrix/")


@respx.mock
def test_webhook_custom_body_and_text_body():
    route = respx.post(HOOK).respond(204)
    cfg = WebhookNotifier(
        type="webhook", url=HOOK, body={"content": "{{ alert.text }}", "n": "{{ alert.failed_count }}"}
    )
    asyncio.run(send_webhook(cfg, alert()))
    body = json.loads(route.calls[0].request.content)
    assert body == {"content": alert().text, "n": 1}

    cfg = WebhookNotifier(type="webhook", url=HOOK, body="{{ alert.subject }}!")
    asyncio.run(send_webhook(cfg, alert()))
    req = route.calls[1].request
    assert req.content.decode() == "1 monitor failed · shop-api · production!"
    assert req.headers["Content-Type"].startswith("text/plain")


@respx.mock
def test_webhook_errors_do_not_leak_url():
    respx.post(HOOK).respond(500)
    r = asyncio.run(send("chat", WebhookNotifier(type="webhook", url=HOOK), alert()))
    assert not r.ok and r.message == "webhook returned HTTP 500"
    respx.post(HOOK).mock(side_effect=httpx.ConnectError("boom https://hooks.test/abc"))
    r = asyncio.run(send("chat", WebhookNotifier(type="webhook", url=HOOK), alert()))
    assert not r.ok and "abc" not in r.message
    r = asyncio.run(send("chat", WebhookNotifier(type="webhook", url=None), alert()))
    assert not r.ok and "url is not set" in r.message
    bad = WebhookNotifier(type="webhook", url=HOOK, body={"x": "{{ alert.nope }}"})
    r = asyncio.run(send("chat", bad, alert()))
    assert not r.ok and "webhook body" in r.message


# --------------------------------------------------------------------------- email


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    offers_starttls = True
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.ssl = (
            host,
            port,
            context is not None and type(self).__name__ == "FakeSMTPSSL",
        )
        self.calls: list[str] = []
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return self.offers_starttls

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg):
        self.sent = msg


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.offers_starttls = True
    FakeSMTP.fail_login = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def test_parse_smtp_url():
    s = parse_smtp_url("smtp://me%40x.com:p%40ss@mail.x.com")
    assert (s.host, s.port, s.implicit_tls, s.username, s.password) == (
        "mail.x.com",
        587,
        False,
        "me@x.com",
        "p@ss",
    )
    s = parse_smtp_url("smtps://mail.x.com?insecure=1")
    assert s.port == 465 and s.implicit_tls and not s.require_tls
    with pytest.raises(Exception, match="smtp_url must look like"):
        parse_smtp_url("https://mail.x.com")


def test_email_starttls_login_and_message(fake_smtp):
    cfg = EmailNotifier(
        type="email", to=["dev@x.com", "ops@x.com"], smtp_url="smtp://bot%40x.com:pw@mail.x.com:2525"
    )
    asyncio.run(send_email(cfg, alert()))
    s = fake_smtp.instances[0]
    assert (s.host, s.port) == ("mail.x.com", 2525)
    assert s.calls == ["ehlo", "starttls", "ehlo", "login:bot@x.com:pw"]
    assert s.sent["Subject"] == "[api-qovrix] 1 monitor failed · shop-api · production"
    assert s.sent["From"] == "bot@x.com" and s.sent["To"] == "dev@x.com, ops@x.com"
    assert "get-order" in s.sent.get_content()


def test_email_implicit_tls_and_sender(fake_smtp):
    cfg = EmailNotifier(
        type="email", to=["d@x.com"], smtp_url="smtps://mail.x.com", **{"from": "alerts@x.com"}
    )
    asyncio.run(send_email(cfg, alert()))
    s = fake_smtp.instances[0]
    assert isinstance(s, FakeSMTPSSL) and s.port == 465 and "starttls" not in s.calls
    assert s.sent["From"] == "alerts@x.com"


def test_email_errors(fake_smtp):
    fake_smtp.offers_starttls = False
    cfg = EmailNotifier(type="email", to=["d@x.com"], smtp_url="smtp://u:p@mail.x.com")
    r = asyncio.run(send("mail", cfg, alert()))
    assert not r.ok and "STARTTLS" in r.message
    fake_smtp.offers_starttls = True
    fake_smtp.fail_login = True
    r = asyncio.run(send("mail", cfg, alert()))
    assert not r.ok and "login failed" in r.message and "p@" not in r.message
    r = asyncio.run(send("mail", EmailNotifier(type="email", to=["d@x.com"]), alert()))
    assert not r.ok and "smtp_url is not set" in r.message


# --------------------------------------------------------------------------- config + CLI

CONFIG = """
version: 1
project: shop-api
environments: { prod: { base_url: "https://api.test" } }
notifiers:
  chat:
    type: webhook
    url: ${env:QOVRIX_HOOK}
  mail:
    type: email
    to: [dev@x.com]
    smtp_url: ${env:QOVRIX_SMTP_UNSET}
defaults:
  notify: [chat]
"""
MONITORS = """
monitors:
  - name: broken
    request: { url: /broken }
  - name: fine
    request: { url: /fine }
  - name: quiet
    notify: []
    request: { url: /broken }
"""


def in_dir(path, *args):
    old = os.getcwd()
    os.chdir(path)
    try:
        return CliRunner().invoke(app, list(args))
    finally:
        os.chdir(old)


def test_unknown_notifier_type_is_a_config_error(make_project):
    root = make_project("notifiers:\n  x: { type: pager }\n")
    with pytest.raises(ConfigError, match=r"notifiers\.x"):
        load_project(root / "api-qovrix.yaml")


@respx.mock
def test_run_notify(make_project, monkeypatch):
    monkeypatch.setenv("QOVRIX_HOOK", HOOK)
    monkeypatch.delenv("QOVRIX_SMTP_UNSET", raising=False)
    respx.get("https://api.test/broken").respond(500)
    respx.get("https://api.test/fine").respond(200)
    hook = respx.post(HOOK).respond(200)
    root = make_project(CONFIG, {"monitors/a.yaml": MONITORS})

    r = in_dir(root, "run")
    assert r.exit_code == 1 and not hook.called  # no --notify, no alert

    r = in_dir(root, "run", "--notify")
    assert r.exit_code == 1
    assert "alert sent → chat" in r.output
    assert "no notifier for: quiet" in r.output
    body = json.loads(hook.calls[0].request.content)
    assert [m["name"] for m in body["failed"]] == ["broken"] and body["total"] == 3

    r = in_dir(root, "run", "fine", "--notify")
    assert r.exit_code == 0 and hook.call_count == 1  # all passed: nothing sent


@respx.mock
def test_notifiers_list_test_and_validate(make_project, monkeypatch):
    monkeypatch.setenv("QOVRIX_HOOK", HOOK)
    monkeypatch.delenv("QOVRIX_SMTP_UNSET", raising=False)
    hook = respx.post(HOOK).respond(200)
    root = make_project(CONFIG, {"monitors/a.yaml": MONITORS})

    r = in_dir(root, "notifiers", "list")
    assert (
        r.exit_code == 0 and "chat" in r.output and "URL not set" in r.output and "broken, fine" in r.output
    )
    assert HOOK not in r.output

    r = in_dir(root, "notifiers", "test", "chat")
    assert r.exit_code == 0 and "test alert sent" in r.output
    assert json.loads(hook.calls[0].request.content)["event"] == "test"

    r = in_dir(root, "notifiers", "test")
    assert r.exit_code == 1 and "smtp_url is not set" in r.output
    assert in_dir(root, "notifiers", "test", "ghost").exit_code == 2

    r = in_dir(root, "validate")
    assert r.exit_code == 0 and "notifier mail has no smtp_url" in r.output
    assert in_dir(root, "validate", "--strict").exit_code == 1
