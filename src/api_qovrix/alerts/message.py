"""The alert sent to notifiers: what failed, why, and where to look."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from api_qovrix.engine.results import MonitorResult

MAX_DRIFT_LINES = 5
Event = Literal["run.failed", "test"]


@dataclass
class FailedMonitor:
    name: str
    service: str
    status: str
    summary: str
    drift: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_result(cls, r: MonitorResult) -> FailedMonitor:
        changes: list[dict[str, str]] = []
        for step in r.steps:
            if step.drift and step.drift.failed:
                changes.extend(
                    {"path": c.path, "severity": c.severity, "message": c.message}
                    for c in step.drift.changes
                    if c.severity != "info"
                )
        return cls(r.name, r.service, r.status, r.failure_summary, changes[:MAX_DRIFT_LINES])


@dataclass
class Alert:
    event: Event
    project: str | None
    environment: str | None
    total: int
    failed: list[FailedMonitor]
    run_url: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat())

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def where(self) -> str:
        return " · ".join(p for p in (self.project, self.environment) if p)

    @property
    def subject(self) -> str:
        if self.event == "test":
            return "Test alert"
        noun = "monitor" if self.failed_count == 1 else "monitors"
        where = f" · {self.where}" if self.where else ""
        return f"{self.failed_count} {noun} failed{where}"

    @property
    def text(self) -> str:
        if self.event == "test":
            where = f" for {self.where}" if self.where else ""
            return f"✅ api-qovrix test alert{where}. Notifications are working."
        head = f"❌ api-qovrix: {self.failed_count} of {self.total} monitors failed"
        if self.where:
            head += f" ({self.where})"
        lines = [head, ""]
        for m in self.failed:
            lines.append(f"• {m.name} [{m.service}] {m.status}: {m.summary}")
            lines.extend(f"    {c['path']}  {c['message']} ({c['severity']})" for c in m.drift)
        if self.run_url:
            lines += ["", f"Run: {self.run_url}"]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(text=self.text, subject=self.subject, failed_count=self.failed_count)
        return data


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


def run_alert(project: str | None, environment: str | None, total: int, failed: list[MonitorResult]) -> Alert:
    return Alert(
        event="run.failed",
        project=project,
        environment=environment,
        total=total,
        failed=[FailedMonitor.from_result(r) for r in failed],
        run_url=ci_run_url(),
    )


def make_test_alert(project: str | None, environment: str | None) -> Alert:
    return Alert(
        event="test", project=project, environment=environment, total=0, failed=[], run_url=ci_run_url()
    )
