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
class Segment:
    """One block of an uptime bar (an hour or a day)."""

    level: str  # up | minor | down | none
    label: str


def segment(label: str, ok: int, total: int) -> Segment:
    if not total:
        return Segment("none", f"{label}: no checks")
    pct = 100.0 * ok / total
    level = "up" if ok == total else ("minor" if pct >= 95 else "down")
    failed = total - ok
    detail = "all checks passed" if not failed else f"{failed} of {total} checks failed"
    return Segment(level, f"{label}: {detail}")


def hour_segments(hours: dict[str, tuple[int, int]], now: datetime, count: int = 24) -> list[Segment]:
    out = []
    for i in range(count - 1, -1, -1):
        t = now - timedelta(hours=i)
        key = t.strftime("%Y-%m-%dT%H")
        ok, total = hours.get(key, (0, 0))
        out.append(segment(t.strftime("%H:00 UTC"), ok, total))
    return out


def day_segments(rows: list[dict[str, Any]], now: datetime, count: int = 30) -> list[Segment]:
    by_day = {r["day"]: (int(r["ok"]), int(r["total"])) for r in rows}
    out = []
    for i in range(count - 1, -1, -1):
        day = (now - timedelta(days=i)).date().isoformat()
        ok, total = by_day.get(day, (0, 0))
        out.append(segment(day, ok, total))
    return out


@dataclass
class MonitorView:
    name: str
    service: str
    description: str | None
    state: str
    note: str
    interval: str
    schedule: str
    last_run: str
    last_run_at: str | None
    last_status: str | None
    last_summary: str | None
    last_duration_ms: float | None
    uptime: str
    tags: list[str]
    failing: int = 0
    alert_after: int = 2
    chart: Chart | None = None
    bars: list[Segment] = field(default_factory=list)

    @property
    def display(self) -> str:
        """Badge style: 'failing' (failed, not down yet) is shown apart from 'down'."""
        return "failing" if self.state != "down" and self.failing else self.state

    def api(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("chart", "last_run", "bars"):
            data.pop(key)
        return data


@dataclass
class ServiceView:
    name: str
    state: str
    monitors: list[MonitorView] = field(default_factory=list)

    @property
    def display(self) -> str:
        if self.state != "down" and any(m.display == "failing" for m in self.monitors):
            return "failing"
        return self.state

    @property
    def summary(self) -> str:
        down = sum(m.state == "down" for m in self.monitors)
        failing = sum(m.display == "failing" for m in self.monitors)
        parts = [f"{len(self.monitors)} monitor{'s' if len(self.monitors) != 1 else ''}"]
        if down:
            parts.append(f"{down} down")
        if failing:
            parts.append(f"{failing} failing")
        return " · ".join(parts)

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
        schedule=f"cron {m.monitor.cron}" if m.monitor.cron else f"every {human_duration(m.interval)}",
        last_run=ago(st.last_run_at if st else None, now),
        last_run_at=st.last_run_at if st else None,
        last_status=st.last_status if st else None,
        last_summary=st.last_summary if st else None,
        last_duration_ms=st.last_duration_ms if st else None,
        uptime=_uptime(uptime.get(m.name)),
        tags=m.tags,
        failing=st.consecutive_failures if st and st.state != "down" else 0,
        alert_after=m.alert_after,
        chart=Chart.build(runs, *chart_size) if runs else None,
    )


async def overview(
    project: Project, storage: Storage, now: datetime, charts: bool = True
) -> list[ServiceView]:
    states = {s.monitor: s for s in await storage.all_states()}
    uptime = await storage.uptime((now - timedelta(hours=24)).isoformat())
    hourly = await storage.hourly((now - timedelta(hours=23)).strftime("%Y-%m-%dT%H")) if charts else {}
    services: dict[str, ServiceView] = {}
    for m in sorted(project.monitors, key=lambda x: (x.service, x.name)):
        runs = await storage.history(m.name, SPARK_RUNS) if charts else None
        view = await monitor_view(m, states.get(m.name), uptime, now, runs)
        if charts:
            view.bars = hour_segments(hourly.get(m.name, {}), now)
        services.setdefault(m.service, ServiceView(m.service, "unknown")).monitors.append(view)
    for svc in services.values():
        svc.state = worst([mv.state for mv in svc.monitors])
    return sorted(services.values(), key=lambda s: (STATE_ORDER.get(s.state, 1), s.name))


def counts(services: list[ServiceView]) -> dict[str, int]:
    out = {"up": 0, "down": 0, "unknown": 0, "paused": 0, "failing": 0, "healthy": 0, "waiting": 0}
    for svc in services:
        for m in svc.monitors:
            out[m.state] = out.get(m.state, 0) + 1
            if m.display == "failing":
                out["failing"] += 1  # failing monitors count once, here, whatever their earlier state
            elif m.state == "up":
                out["healthy"] += 1
            elif m.state in ("unknown", "paused"):
                out["waiting"] += 1
    return out


@dataclass
class Banner:
    level: str  # up | failing | down | unknown
    title: str
    detail: str


def banner(services: list[ServiceView]) -> Banner:
    monitors = [m for s in services for m in s.monitors]
    down = [m.name for m in monitors if m.state == "down"]
    failing = [m.name for m in monitors if m.display == "failing"]
    checked = [m for m in monitors if m.state in ("up", "down")]

    def names(items: list[str]) -> str:
        return ", ".join(items[:5]) + (f" and {len(items) - 5} more" if len(items) > 5 else "")

    if down:
        noun = "monitor is" if len(down) == 1 else "monitors are"
        detail = names(down) + (f" · also failing: {names(failing)}" if failing else "")
        return Banner("down", f"{len(down)} {noun} down", detail)
    if failing:
        noun = "monitor is" if len(failing) == 1 else "monitors are"
        return Banner("failing", f"{len(failing)} {noun} failing", f"{names(failing)} (not down yet)")
    if not checked:
        return Banner("unknown", "Waiting for the first checks", f"{len(monitors)} monitors scheduled")
    return Banner("up", "All monitors are up", f"{len(checked)} monitors checked")
