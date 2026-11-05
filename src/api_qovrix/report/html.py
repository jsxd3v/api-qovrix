"""A single self-contained HTML report of a run (`api-qovrix run --html report.html`).

No scripts and no external files: the dashboard's stylesheet is inlined, so the file can be opened offline,
attached to a CI run or emailed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from jinja2 import Environment, select_autoescape

from api_qovrix import __version__
from api_qovrix.alerts.message import ci_run_url
from api_qovrix.engine.results import RunSummary
from api_qovrix.report.output import summary_to_dict

STATE = {"pass": "up", "fail": "down", "error": "down", "skipped": "paused"}


def _read(package: str, name: str) -> str:
    return (resources.files(package) / name).read_text(encoding="utf-8")


def render_html(summary: RunSummary, project: str | None = None, now: datetime | None = None) -> str:
    report: dict[str, Any] = summary_to_dict(summary)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in report["monitors"]:
        groups[m["service"]].append(m)
    order = {"error": 0, "fail": 1, "pass": 2, "skipped": 3}
    services = sorted(
        ((name, sorted(ms, key=lambda m: (order[m["status"]], m["name"]))) for name, ms in groups.items()),
        key=lambda item: (min(order[m["status"]] for m in item[1]), item[0]),
    )
    env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(_read("api_qovrix.report", "templates/report.html"))
    generated = (now or datetime.now(UTC)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M UTC")
    return template.render(
        title=f"{project or 'api-qovrix'} · run report",
        report=report,
        services=services,
        state=STATE,
        css=_read("api_qovrix.web", "static/app.css"),
        generated=generated,
        run_url=ci_run_url(),
        version=__version__,
    )
