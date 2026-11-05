"""Send an alert by email (SMTP) or webhook (HTTP POST with JSON)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from api_qovrix.alerts.message import Alert, run_alert
from api_qovrix.config.models import EmailNotifier, NotifierConfig, WebhookNotifier
from api_qovrix.config.templating import render_value
from api_qovrix.engine.http import USER_AGENT
from api_qovrix.engine.results import MonitorResult
from api_qovrix.errors import QovrixError, TemplateError


class NotifyError(QovrixError):
    pass


@dataclass
class SendResult:
    name: str
    ok: bool
    message: str = ""


# --------------------------------------------------------------------------- webhook


def webhook_body(cfg: WebhookNotifier, alert: Alert) -> Any:
    data = alert.to_dict()
    if cfg.body is None:
        return data
    try:
        return render_value(cfg.body, {"alert": data})
    except TemplateError as exc:
        raise NotifyError(f"webhook body: {exc}") from None


async def send_webhook(
    cfg: WebhookNotifier, alert: Alert, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    if not cfg.url:
        raise NotifyError("url is not set (is its environment variable set?)")
    body = webhook_body(cfg, alert)
    headers = {"User-Agent": USER_AGENT, **cfg.headers}
    kwargs: dict[str, Any] = {"timeout": cfg.timeout}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            if isinstance(body, str):
                headers.setdefault("Content-Type", "text/plain; charset=utf-8")
                resp = await client.post(cfg.url, content=body.encode(), headers=headers)
            else:
                resp = await client.post(cfg.url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise NotifyError(f"webhook request failed: {type(exc).__name__}") from None
    if resp.status_code >= 400:
        raise NotifyError(f"webhook returned HTTP {resp.status_code}")


# --------------------------------------------------------------------------- email


@dataclass
class SmtpSettings:
    host: str
    port: int
    implicit_tls: bool
    username: str | None
    password: str | None
    require_tls: bool


def parse_smtp_url(url: str) -> SmtpSettings:
    """smtp://user:pass@host:587 (STARTTLS) or smtps://user:pass@host:465 (TLS).

    `?insecure=true` allows plain SMTP without TLS (e.g. a local relay).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
        raise NotifyError(
            "smtp_url must look like smtp://user:password@host:587 or smtps://user:password@host:465"
        )
    implicit = parts.scheme == "smtps"
    query = parse_qs(parts.query)
    insecure = query.get("insecure", ["false"])[0].lower() in ("1", "true", "yes")
    return SmtpSettings(
        host=parts.hostname,
        port=parts.port or (465 if implicit else 587),
        implicit_tls=implicit,
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
        require_tls=not insecure,
    )


def build_email(cfg: EmailNotifier, alert: Alert, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{cfg.subject_prefix} {alert.subject}".strip()
    msg["From"] = sender
    msg["To"] = ", ".join(cfg.to)
    msg.set_content(alert.text)
    return msg


def _send_email_sync(cfg: EmailNotifier, alert: Alert, timeout: float) -> None:
    if not cfg.smtp_url:
        raise NotifyError("smtp_url is not set (is its environment variable set?)")
    s = parse_smtp_url(cfg.smtp_url)
    sender = cfg.sender or s.username or "api-qovrix@localhost"
    if "@" not in sender:
        sender = f"{sender}@{s.host}"
    msg = build_email(cfg, alert, sender)
    context = ssl.create_default_context()
    try:
        server: smtplib.SMTP
        if s.implicit_tls:
            server = smtplib.SMTP_SSL(s.host, s.port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(s.host, s.port, timeout=timeout)
        with server:
            server.ehlo()
            if not s.implicit_tls:
                if server.has_extn("starttls"):
                    server.starttls(context=context)
                    server.ehlo()
                elif s.require_tls:
                    raise NotifyError(
                        "the SMTP server does not offer STARTTLS (add ?insecure=true to allow it)"
                    )
            if s.username and s.password:
                server.login(s.username, s.password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise NotifyError("SMTP login failed (check the user and password in smtp_url)") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"email could not be sent: {type(exc).__name__}: {exc}") from None


async def send_email(cfg: EmailNotifier, alert: Alert, timeout: float = 20.0) -> None:
    await asyncio.to_thread(_send_email_sync, cfg, alert, timeout)


# --------------------------------------------------------------------------- dispatch


async def send(
    name: str, cfg: NotifierConfig, alert: Alert, transport: httpx.AsyncBaseTransport | None = None
) -> SendResult:
    try:
        if isinstance(cfg, WebhookNotifier):
            await send_webhook(cfg, alert, transport)
        else:
            await send_email(cfg, alert)
    except QovrixError as exc:
        return SendResult(name, False, str(exc))
    return SendResult(name, True)


async def send_all(
    notifiers: dict[str, NotifierConfig],
    alerts: Iterable[tuple[str, Alert]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[SendResult]:
    """Send each (notifier name, alert) pair concurrently."""
    tasks = [send(name, notifiers[name], alert, transport) for name, alert in alerts]
    return list(await asyncio.gather(*tasks))


def plan_run_alerts(
    project_name: str | None,
    environment: str | None,
    results: list[MonitorResult],
    notify_map: dict[str, list[str]],
) -> tuple[list[tuple[str, Alert]], list[str]]:
    """One alert per notifier, listing only the failed monitors routed to it.

    Returns (notifier name, alert) pairs and the failed monitors that have no notifier.
    """
    failed = [r for r in results if r.status in ("fail", "error")]
    by_notifier: dict[str, list[MonitorResult]] = {}
    unrouted: list[str] = []
    for r in failed:
        targets = notify_map.get(r.name, [])
        if not targets:
            unrouted.append(r.name)
        for name in targets:
            by_notifier.setdefault(name, []).append(r)
    plan = [
        (name, run_alert(project_name, environment, len(results), monitors))
        for name, monitors in sorted(by_notifier.items())
    ]
    return plan, unrouted
