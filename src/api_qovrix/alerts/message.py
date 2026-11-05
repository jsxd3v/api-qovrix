"""The alert sent to notifiers: what failed, why, and where to look."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from api_qovrix.engine.results import MonitorResult

MAX_DRIFT_LINES = 5
Event = Literal["run.failed", "incident", "test"]


@dataclass
class FailedMonitor:
    name: str
    service: str
    status: str
    summary: str
    drift: list[dict[str, str]] = field(default_factory=list)
    since: str | None = None  # ISO time the monitor went down (incidents)

    @classmethod
    def from_result(cls, r: MonitorResult, since: str | None = None) -> FailedMonitor:
        changes: list[dict[str, str]] = []
        for step in r.steps:
            if step.drift and step.drift.failed:
                changes.extend(
                    {"path": c.path, "severity": c.severity, "message": c.message}
                    for c in step.drift.changes
                    if c.severity != "info"
                )
        return cls(r.name, r.service, r.status, r.failure_summary, changes[:MAX_DRIFT_LINES], since)


@dataclass
class RecoveredMonitor:
    name: str
    service: str
    down_for: str


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


@dataclass
class Alert:
    event: Event
    project: str | None
    environment: str | None
    total: int
    failed: list[FailedMonitor]
    run_url: str | None = None
    service: str | None = None
    recovered: list[RecoveredMonitor] = field(default_factory=list)
    reminder: bool = False
    dashboard_url: str | None = None
    brand: str | None = None
    accent: str = "#5b4fd6"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat())

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def kind(self) -> str:
        """down | reminder | recovered | failed | test: decides colors and headlines."""
        if self.event == "test":
            return "test"
        if self.event == "run.failed":
            return "failed"
        if self.failed:
            return "reminder" if self.reminder else "down"
        return "recovered"

    def monitor_url(self, name: str) -> str | None:
        return f"{self.dashboard_url.rstrip('/')}/monitors/{quote(name)}" if self.dashboard_url else None

    @property
    def link(self) -> str | None:
        """Where to look: the dashboard (or the single monitor), else the CI run."""
        if self.dashboard_url:
            if len(self.failed) == 1 and not self.recovered:
                return self.monitor_url(self.failed[0].name)
            return self.dashboard_url.rstrip("/") + "/"
        return self.run_url

    @property
    def where(self) -> str:
        return " · ".join(p for p in (self.project, self.environment) if p)

    @property
    def subject(self) -> str:
        if self.event == "test":
            return "Test alert"
        if self.event == "incident":
            parts = []
            if self.failed:
                noun = "monitor" if self.failed_count == 1 else "monitors"
                parts.append(f"{self.failed_count} {noun} {'still ' if self.reminder else ''}down")
            if self.recovered:
                parts.append(f"{len(self.recovered)} recovered")
            where = f" · {self.where}" if self.where else ""
            return f"{self.service}: {', '.join(parts)}{where}"
        noun = "monitor" if self.failed_count == 1 else "monitors"
        where = f" · {self.where}" if self.where else ""
        return f"{self.failed_count} {noun} failed{where}"

    @property
    def text(self) -> str:
        if self.event == "test":
            where = f" for {self.where}" if self.where else ""
            return f"✅ api-qovrix test alert{where}. Notifications are working."
        if self.event == "incident":
            return self._incident_text()
        head = f"❌ api-qovrix: {self.failed_count} of {self.total} monitors failed"
        if self.where:
            head += f" ({self.where})"
        lines = [head, ""]
        for m in self.failed:
            lines.append(f"• {m.name} [{m.service}] {m.status}: {m.summary}")
            lines.extend(f"    {c['path']}  {c['message']} ({c['severity']})" for c in m.drift)
        if self.run_url:
            lines += ["", f"Run: {self.run_url}"]
        if self.dashboard_url:
            lines += [f"Dashboard: {self.dashboard_url.rstrip('/')}/"]
        return "\n".join(lines)

    def _incident_text(self) -> str:
        where = f" ({self.where})" if self.where else ""
        lines: list[str] = []
        if self.failed:
            noun = "monitor" if self.failed_count == 1 else "monitors"
            state = "still down" if self.reminder else "down"
            lines.append(f"🔴 api-qovrix: {self.failed_count} {noun} {state} in {self.service}{where}")
            lines.append("")
            for m in self.failed:
                since = f" (since {short_time(m.since)})" if m.since else ""
                lines.append(f"• {m.name} {m.status}{since}: {m.summary}")
                lines.extend(f"    {c['path']}  {c['message']} ({c['severity']})" for c in m.drift)
        if self.recovered:
            if lines:
                lines.append("")
                lines.append("✅ Recovered:")
            else:
                n = len(self.recovered)
                noun = "monitor" if n == 1 else "monitors"
                lines.append(f"✅ api-qovrix: {n} {noun} recovered in {self.service}{where}")
                lines.append("")
            lines.extend(f"• {r.name} (was down {r.down_for})" for r in self.recovered)
        if self.link:
            lines += ["", f"Open: {self.link}"]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            text=self.text,
            subject=self.subject,
            failed_count=self.failed_count,
            kind=self.kind,
            link=self.link,
        )
        return data


def short_time(ts: str) -> str:
    """'2026-11-05T10:05:00+00:00' -> '10:05 UTC' (same day) — compact for phones."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return ts
    today = datetime.now(UTC).date()
    return dt.strftime("%H:%M UTC") if dt.date() == today else dt.strftime("%Y-%m-%d %H:%M UTC")


def presentation(config: Any) -> dict[str, Any]:
    """Dashboard link and branding for alerts, from the project config."""
    return {
        "dashboard_url": config.server.public_url,
        "brand": config.branding.name or config.project,
        "accent": config.branding.accent,
    }


def ci_run_url() -> str | None:
    """Link to the CI run: GitHub Actions automatically, other CIs via API_QOVRIX_RUN_URL."""
    if url := os.environ.get("API_QOVRIX_RUN_URL"):
        return url
    server, repo, run_id = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def run_alert(
    project: str | None, environment: str | None, total: int, failed: list[MonitorResult], **look: Any
) -> Alert:
    return Alert(
        event="run.failed",
        project=project,
        environment=environment,
        total=total,
        failed=[FailedMonitor.from_result(r) for r in failed],
        run_url=ci_run_url(),
        **look,
    )


def make_test_alert(project: str | None, environment: str | None, **look: Any) -> Alert:
    return Alert(
        event="test",
        project=project,
        environment=environment,
        total=0,
        failed=[],
        run_url=ci_run_url(),
        **look,
    )
