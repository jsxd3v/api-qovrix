"""Collect what the dashboard and the REST API show, from config + storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from api_qovrix.alerts.message import human_duration

__all__ = ["human_duration"]
from api_qovrix.config.loader import Project, ResolvedMonitor
from api_qovrix.server.storage import MonitorState, Storage

STATE_ORDER = {"down": 0, "unknown": 1, "up": 2, "paused": 3}
SPARK_RUNS = 30


def ago_iso(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).isoformat()


def ago(ts: str | None, now: datetime) -> str:
    if not ts:
        return "never"
    seconds = (now - datetime.fromisoformat(ts)).total_seconds()
    if seconds < 5:
        return "just now"
    return f"{human_duration(seconds)} ago"


@dataclass
class Chart:
    """An SVG line chart of response times; failed runs are marked."""

    width: int
    height: int
    points: str
    failures: list[tuple[float, float]]
    max_ms: float
    count: int

    @classmethod
    def build(cls, runs: list[dict[str, Any]], width: int, height: int, pad: int = 4) -> Chart:
        ordered = list(reversed(runs))  # oldest first
        values = [float(r["duration_ms"] or 0) for r in ordered]
        top = max(values, default=0.0) or 1.0
        n = len(values)
        step = (width - 2 * pad) / max(n - 1, 1)
        coords = [
            (pad + i * step if n > 1 else width / 2, height - pad - (v / top) * (height - 2 * pad))
            for i, v in enumerate(values)
        ]
        failures = [c for c, r in zip(coords, ordered, strict=True) if r["status"] in ("fail", "error")]
        return cls(
            width=width,
            height=height,
            points=" ".join(f"{x:.1f},{y:.1f}" for x, y in coords),
            failures=failures,
            max_ms=top,
            count=n,
        )


@dataclass
class MonitorView:
    name: str
    service: str
    description: str | None
    state: str
    note: str
    interval: str
    last_run: str
    last_run_at: str | None
    last_status: str | None
    last_summary: str | None
    last_duration_ms: float | None
    uptime: str
    tags: list[str]
    chart: Chart | None = None

    def api(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("chart")
        data.pop("last_run")
        return data


@dataclass
class ServiceView:
    name: str
    state: str
    monitors: list[MonitorView] = field(default_factory=list)

    def api(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "monitors": [m.api() for m in self.monitors]}


def _note(m: ResolvedMonitor, st: MonitorState | None) -> str:
    if st is None:
        return "not checked yet"
    if st.state == "down":
        return "down"
    if st.consecutive_failures:
        return f"failing {st.consecutive_failures}/{m.alert_after}"
    return st.state


def _uptime(stats: tuple[int, int] | None) -> str:
    if not stats or not stats[1]:
        return "—"
    pct = 100.0 * stats[0] / stats[1]
    return f"{pct:.1f}%" if pct < 100 else "100%"


def worst(states: list[str]) -> str:
    if not states:
        return "unknown"
    w = min(states, key=lambda s: STATE_ORDER.get(s, 1))
    return "up" if w == "paused" and any(s == "up" for s in states) else w


async def monitor_view(
    m: ResolvedMonitor,
    st: MonitorState | None,
    uptime: dict[str, tuple[int, int]],
    now: datetime,
    runs: list[dict[str, Any]] | None = None,
    chart_size: tuple[int, int] = (120, 28),
) -> MonitorView:
    return MonitorView(
        name=m.name,
        service=m.service,
        description=m.monitor.description,
        state=st.state if st else "unknown",
        note=_note(m, st),
        interval=human_duration(m.interval),
        last_run=ago(st.last_run_at if st else None, now),
        last_run_at=st.last_run_at if st else None,
        last_status=st.last_status if st else None,
        last_summary=st.last_summary if st else None,
        last_duration_ms=st.last_duration_ms if st else None,
        uptime=_uptime(uptime.get(m.name)),
        tags=m.tags,
        chart=Chart.build(runs, *chart_size) if runs else None,
    )


async def overview(
    project: Project, storage: Storage, now: datetime, charts: bool = True
) -> list[ServiceView]:
    states = {s.monitor: s for s in await storage.all_states()}
    uptime = await storage.uptime((now - timedelta(hours=24)).isoformat())
    services: dict[str, ServiceView] = {}
    for m in sorted(project.monitors, key=lambda x: (x.service, x.name)):
        runs = await storage.history(m.name, SPARK_RUNS) if charts else None
        view = await monitor_view(m, states.get(m.name), uptime, now, runs)
        services.setdefault(m.service, ServiceView(m.service, "unknown")).monitors.append(view)
    for svc in services.values():
        svc.state = worst([mv.state for mv in svc.monitors])
    return sorted(services.values(), key=lambda s: (STATE_ORDER.get(s.state, 1), s.name))


def counts(services: list[ServiceView]) -> dict[str, int]:
    out = {"up": 0, "down": 0, "unknown": 0, "paused": 0}
    for svc in services:
        for m in svc.monitors:
            out[m.state] = out.get(m.state, 0) + 1
    return out
