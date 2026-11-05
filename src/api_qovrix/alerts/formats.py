"""Rich alert formats: HTML email, a colored card ("embed") and a block layout ("blocks").

Both chat formats respect common incoming-webhook limits, so long alerts are shortened instead of rejected.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from jinja2 import Environment, select_autoescape

from api_qovrix.alerts.message import Alert, short_time

COLORS = {
    "down": "#c92a2a",
    "failed": "#c92a2a",
    "reminder": "#b25e09",
    "recovered": "#0f8a5f",
    "test": "#2459c7",
}
LABELS = {
    "down": "Down",
    "failed": "Monitors failed",
    "reminder": "Still down",
    "recovered": "Recovered",
    "test": "Test",
}

# embed limits (characters): title 256, description 4096, 25 fields, name 256, value 1024, total 6000
EMBED_FIELDS = 10
EMBED_TOTAL = 5800
# block limits: header text 150, section text 3000, 50 blocks
BLOCK_SECTIONS = 20


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def headline(a: Alert) -> str:
    if a.event == "test":
        return "Test alert: notifications are working"
    if a.event == "run.failed":
        noun = "monitor" if a.failed_count == 1 else "monitors"
        return f"{a.failed_count} of {a.total} {noun} failed"
    parts = []
    if a.failed:
        noun = "monitor" if a.failed_count == 1 else "monitors"
        parts.append(f"{a.failed_count} {noun} {'still ' if a.reminder else ''}down")
    if a.recovered:
        noun = "monitor" if len(a.recovered) == 1 else "monitors"
        parts.append(f"{len(a.recovered)} {noun} recovered")
    return f"{a.service}: " + ", ".join(parts)


def _env() -> Environment:
    env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.globals["short_time"] = short_time
    return env


def email_html(a: Alert) -> str:
    template = (resources.files("api_qovrix.alerts") / "templates" / "email.html").read_text(encoding="utf-8")
    kind = a.kind
    return (
        _env().from_string(template).render(a=a, color=COLORS[kind], label=LABELS[kind], headline=headline(a))
    )


def _reason(m: Any, a: Alert) -> str:
    lines = [m.summary or m.status]
    lines += [f"`{c['path']}` {c['message']}" for c in m.drift[:3]]
    if m.since:
        lines.append(f"since {short_time(m.since)}")
    url = a.monitor_url(m.name)
    if url:
        lines.append(f"[Open]({url})")
    return "\n".join(lines)


def embed(a: Alert) -> dict[str, Any]:
    """A card with a colored side bar: title, one field per monitor, timestamp and link."""
    kind = a.kind
    fields: list[dict[str, Any]] = []
    for m in a.failed[:EMBED_FIELDS]:
        fields.append({"name": _cut(f"{m.name} · {m.status}", 256), "value": _cut(_reason(m, a), 1024)})
    room = EMBED_FIELDS - len(fields)
    for r in a.recovered[:room]:
        fields.append(
            {"name": _cut(f"✅ {r.name}", 256), "value": _cut(f"recovered, was down {r.down_for}", 1024)}
        )
    hidden = len(a.failed) + len(a.recovered) - len(fields)
    if hidden > 0:
        fields.append({"name": f"+{hidden} more", "value": "See the dashboard for the full list."})
    if a.event == "test":
        description = "Notifications are working."
    else:
        description = " · ".join(p for p in (a.where, LABELS[kind]) if p)
    card: dict[str, Any] = {
        "title": _cut(headline(a), 256),
        "description": _cut(description, 4096),
        "color": int(COLORS[kind].lstrip("#"), 16),
        "fields": fields,
        "timestamp": a.timestamp,
        "footer": {"text": _cut(f"api-qovrix{' · ' + a.brand if a.brand else ''}", 2048)},
    }
    if a.link:
        card["url"] = a.link

    def size(c: dict[str, Any]) -> int:
        return (
            len(c["title"])
            + len(c["description"])
            + len(c["footer"]["text"])
            + sum(len(f["name"]) + len(f["value"]) for f in c["fields"])
        )

    while size(card) > EMBED_TOTAL and len(card["fields"]) > 1:
        card["fields"].pop(-2 if card["fields"][-1]["name"].startswith("+") else -1)
    return {"embeds": [card]}


def _mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def blocks(a: Alert) -> dict[str, Any]:
    """Header, one section per monitor, a context line and a button (with a plain-text fallback)."""
    kind = a.kind
    emoji = {"down": "🔴", "failed": "🔴", "reminder": "🟠", "recovered": "✅", "test": "🔵"}[kind]
    out: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _cut(f"{emoji} {headline(a)}", 150)}}
    ]
    for m in a.failed[:BLOCK_SECTIONS]:
        lines = [f"*{_mrkdwn(m.name)}* · `{m.status}`", _mrkdwn(m.summary or "")]
        lines += [f"`{_mrkdwn(c['path'])}` {_mrkdwn(c['message'])}" for c in m.drift[:3]]
        if m.since:
            lines.append(f"_since {short_time(m.since)}_")
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _cut("\n".join(lines), 3000)}})
    if a.recovered:
        names = "\n".join(
            f"✅ *{_mrkdwn(r.name)}*: was down {r.down_for}" for r in a.recovered[:BLOCK_SECTIONS]
        )
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _cut(names, 3000)}})
    hidden = max(0, len(a.failed) - BLOCK_SECTIONS)
    context = " · ".join(
        p for p in (a.where, f"+{hidden} more" if hidden else "", short_time(a.timestamp)) if p
    )
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _cut(_mrkdwn(context), 3000)}]})
    if a.link:
        label = "Open dashboard" if a.dashboard_url else "Open the CI run"
        out.append(
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": label}, "url": a.link}
                ],
            }
        )
    return {"text": _cut(f"{emoji} {headline(a)}", 3000), "blocks": out[:50]}
