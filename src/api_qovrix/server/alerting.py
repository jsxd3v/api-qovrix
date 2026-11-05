"""Alert rules for `serve`.

A monitor is "down" after `alert_after` failures in a row and "up" again after one success. Only state
changes alert: down, recovered, and (optionally) a reminder every `alert_repeat` while still down. Changes of
the same service that happen close together are grouped into one message per notifier.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

from api_qovrix.alerts.message import (
    Alert,
    FailedMonitor,
    RecoveredMonitor,
    ci_run_url,
    human_duration,
    presentation,
)
from api_qovrix.alerts.notifiers import SendResult, send_all
from api_qovrix.config.loader import Project, ResolvedMonitor
from api_qovrix.engine.results import MonitorResult
from api_qovrix.server.storage import MonitorState, Storage

Kind = Literal["down", "reminder", "recovered"]
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


@dataclass
class Transition:
    kind: Kind
    monitor: ResolvedMonitor
    result: MonitorResult
    down_for: float = 0.0
    since: str | None = None


def next_state(
    state: MonitorState, m: ResolvedMonitor, result: MonitorResult, now: datetime
) -> tuple[MonitorState, Transition | None]:
    """Pure rule: the new state after one result, and the alert-worthy change (if any)."""
    ts = now.isoformat()
    new = MonitorState(**{**state.__dict__})
    new.service = m.service
    new.last_run_at = ts
    new.last_status = result.status
    new.last_summary = result.failure_summary
    new.last_duration_ms = round(result.duration_ms, 1)
    transition: Transition | None = None

    if result.status == "skipped":
        new.state, new.consecutive_failures = "paused", 0
        if state.state != "paused":
            new.since = ts
        return new, None

    if result.status == "pass":
        new.consecutive_failures = 0
        if state.state == "down":
            since = parse(state.since) or now
            transition = Transition("recovered", m, result, (now - since).total_seconds())
        if state.state != "up":
            new.state, new.since = "up", ts
        return new, transition

    new.consecutive_failures = state.consecutive_failures + 1
    if state.state != "down":
        if new.consecutive_failures >= m.alert_after:
            new.state, new.since = "down", ts
            transition = Transition("down", m, result, since=ts)
        elif state.state == "paused":
            new.state, new.since = "unknown", ts  # failing, but not down yet
        return new, transition

    last_alert = parse(state.last_alert_at)
    if m.alert_repeat and last_alert and (now - last_alert).total_seconds() >= m.alert_repeat:
        transition = Transition("reminder", m, result, since=state.since)
    return new, transition


class AlertManager:
    def __init__(
        self,
        project: Project,
        storage: Storage,
        clock: Clock = utcnow,
        transport: httpx.AsyncBaseTransport | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.project = project
        self.storage = storage
        self.clock = clock
        self.transport = transport
        self.log = log
        self.pending: list[Transition] = []

    async def process(self, m: ResolvedMonitor, result: MonitorResult) -> Transition | None:
        """Update state and incidents for one result; queue the change for the next flush."""
        now = self.clock()
        state = await self.storage.get_state(m.name) or MonitorState(m.name, m.service)
        new, transition = next_state(state, m, result, now)
        await self.storage.save_state(new)
        if transition is None:
            return None
        if transition.kind == "down":
            await self.storage.open_incident(m.name, m.service, now.isoformat(), result.failure_summary)
        elif transition.kind == "recovered":
            await self.storage.resolve_incident(m.name, now.isoformat())
        if m.notify:
            self.pending.append(transition)
        return transition

    def build(self) -> list[tuple[str, Alert]]:
        """Group pending changes into one alert per (notifier, service)."""
        groups: dict[tuple[str, str], list[Transition]] = {}
        for t in self.pending:
            for name in t.monitor.notify:
                groups.setdefault((name, t.monitor.service), []).append(t)
        out: list[tuple[str, Alert]] = []
        for (name, service), items in sorted(groups.items()):
            failing = [t for t in items if t.kind in ("down", "reminder")]
            recovered = [t for t in items if t.kind == "recovered"]
            alert = Alert(
                event="incident",
                project=self.project.config.project,
                environment=self.project.environment,
                total=len([m for m in self.project.monitors if m.service == service]),
                failed=[FailedMonitor.from_result(t.result, t.since) for t in failing],
                recovered=[
                    RecoveredMonitor(t.monitor.name, service, human_duration(t.down_for)) for t in recovered
                ],
                service=service,
                reminder=bool(failing) and all(t.kind == "reminder" for t in failing),
                run_url=ci_run_url(),
                **presentation(self.project.config),
            )
            out.append((name, alert))
        return out

    async def flush(self) -> list[SendResult]:
        if not self.pending:
            return []
        plan = self.build()
        alerted = {t.monitor.name for t in self.pending if t.kind in ("down", "reminder")}
        self.pending = []
        results = await send_all(self.project.config.notifiers, plan, self.transport)
        for (name, alert), r in zip(plan, results, strict=True):
            if r.ok:
                self.log(f"alert sent → {name}: {alert.subject}")
            else:
                self.log(f"alert failed → {name}: {r.message}")
        now = self.clock().isoformat()
        for monitor in alerted:
            state = await self.storage.get_state(monitor)
            if state:
                state.last_alert_at = now
                await self.storage.save_state(state)
        return results
