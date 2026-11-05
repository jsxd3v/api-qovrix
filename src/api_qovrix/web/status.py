"""Data for the public status page.

Only what the operator chose to publish leaves this module: public service names, states, daily uptime and
incident times. Monitor names, URLs, error messages and drift details never do.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from api_qovrix.alerts.message import human_duration
from api_qovrix.config.loader import Project
from api_qovrix.config.models import StatusNotice
from api_qovrix.server.storage import Storage

ServiceState = Literal["operational", "partial_outage", "major_outage", "maintenance", "unknown"]
LABELS: dict[str, str] = {
    "operational": "Operational",
    "partial_outage": "Partial outage",
    "major_outage": "Major outage",
    "maintenance": "Maintenance",
    "unknown": "No data yet",
}
NOTICE_LABELS = {
    "investigating": "Investigating",
    "identified": "Identified",
    "monitoring": "Monitoring",
    "resolved": "Resolved",
    "maintenance": "Maintenance",
}
MAX_INCIDENTS = 20


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass
class DayBar:
    day: str
    level: Literal["up", "minor", "down", "none"]
    label: str


@dataclass
class PublicService:
    id: str
    name: str
    description: str | None
    state: ServiceState
    label: str
    uptime: str
    bars: list[DayBar] = field(default_factory=list)


@dataclass
class PublicIncident:
    kind: Literal["detected", "notice"]
    title: str
    services: list[str]
    started: str
    resolved: str | None
    duration: str
    status: str
    message: str | None = None

    @property
    def ongoing(self) -> bool:
        return self.resolved is None


@dataclass
class StatusView:
    title: str
    description: str | None
    state: Literal["operational", "partial_outage", "major_outage", "maintenance", "unknown"]
    headline: str
    services: list[PublicService]
    active: list[PublicIncident]
    history: list[PublicIncident]
    days: int
    updated: str

    def api(self) -> dict[str, Any]:
        def inc(i: PublicIncident) -> dict[str, Any]:
            return {**asdict(i), "ongoing": i.ongoing}

        return {
            "title": self.title,
            "state": self.state,
            "headline": self.headline,
            "updated": self.updated,
            "services": [
                {
                    "id": s.id,
                    "name": s.name,
                    "state": s.state,
                    "uptime": s.uptime,
                    "days": [{"day": b.day, "level": b.level} for b in s.bars],
                }
                for s in self.services
            ],
            "incidents": [inc(i) for i in self.active + self.history],
        }


def _bars(rows: list[dict[str, Any]], days: int, today: datetime) -> tuple[list[DayBar], str]:
    by_day: dict[str, list[int]] = {}
    for r in rows:
        acc = by_day.setdefault(r["day"], [0, 0])
        acc[0] += int(r["ok"])
        acc[1] += int(r["total"])
    bars = []
    ok_sum = total_sum = 0
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).date().isoformat()
        ok, total = by_day.get(day, [0, 0])
        ok_sum, total_sum = ok_sum + ok, total_sum + total
        if not total:
            bars.append(DayBar(day, "none", f"{day}: no data"))
            continue
        pct = 100.0 * ok / total
        level: Literal["up", "minor", "down"] = "up" if ok == total else ("minor" if pct >= 95 else "down")
        bars.append(DayBar(day, level, f"{day}: {pct:.2f}% uptime"))
    uptime = "—" if not total_sum else (f"{100.0 * ok_sum / total_sum:.2f}%".replace("100.00%", "100%"))
    return bars, uptime


@dataclass
class _Span:
    start: datetime
    end: datetime | None  # None: still ongoing


def _merge_detected(
    incidents: list[Any], public: dict[str, str], since: datetime, now: datetime
) -> list[PublicIncident]:
    """Outages of monitors in public services, merged per service when they overlap."""
    spans: dict[str, list[_Span]] = {}
    for inc in sorted(incidents, key=lambda i: i.opened_at):
        if inc.service not in public:
            continue
        start = _dt(inc.opened_at)
        end = _dt(inc.resolved_at) if inc.resolved_at else None
        if end is not None and end < since:
            continue
        merged = spans.setdefault(inc.service, [])
        last = merged[-1] if merged else None
        if last is not None and (last.end is None or start <= last.end):
            last.end = None if end is None or last.end is None else max(last.end, end)
        else:
            merged.append(_Span(start, end))
    out = []
    for service, ranges in spans.items():
        for span in ranges:
            out.append(
                PublicIncident(
                    kind="detected",
                    title=f"{public[service]} outage",
                    services=[public[service]],
                    started=span.start.isoformat(),
                    resolved=span.end.isoformat() if span.end else None,
                    duration=human_duration(((span.end or now) - span.start).total_seconds()),
                    status="Resolved" if span.end else "Ongoing",
                )
            )
    return out


def _notice(n: StatusNotice, public: dict[str, str], now: datetime) -> PublicIncident:
    start = _dt(n.started)
    end = _dt(n.resolved) if n.resolved else None
    names = [public[s] for s in n.services] if n.services else ["All services"]
    return PublicIncident(
        kind="notice",
        title=n.title,
        services=names,
        started=start.isoformat(),
        resolved=end.isoformat() if end else None,
        duration=human_duration(((end or now) - start).total_seconds()),
        status=NOTICE_LABELS[n.status] if end is None else "Resolved",
        message=n.message,
    )


async def build(project: Project, storage: Storage, now: datetime) -> StatusView:
    cfg = project.config
    page = cfg.status_page
    name = cfg.branding.name or cfg.project or "Service"
    public = {s.service: s.name or s.service for s in page.services}
    monitors_by_service: dict[str, list[str]] = {}
    for m in project.monitors:
        if m.service in public and m.monitor.enabled:
            monitors_by_service.setdefault(m.service, []).append(m.name)

    states = {s.monitor: s.state for s in await storage.all_states()}
    since = now - timedelta(days=page.days)
    daily = await storage.daily(since.date().isoformat())
    notices = [_notice(n, public, now) for n in page.notices if _dt(n.started) <= now]
    maintenance_now = {
        s
        for n in page.notices
        if n.status == "maintenance" and n.resolved is None and _dt(n.started) <= now
        for s in (n.services or list(public))
    }

    services = []
    for svc in page.services:
        names = monitors_by_service.get(svc.service, [])
        mstates = [states.get(n, "unknown") for n in names]
        if names and all(s == "down" for s in mstates):
            state: ServiceState = "major_outage"
        elif any(s == "down" for s in mstates):
            state = "partial_outage"
        elif svc.service in maintenance_now:
            state = "maintenance"
        elif not mstates or all(s in ("unknown", "paused") for s in mstates):
            state = "unknown"
        else:
            state = "operational"
        rows = [r for r in daily if r["monitor"] in names]
        bars, uptime = _bars(rows, page.days, now)
        services.append(
            PublicService(
                id=svc.service,
                name=public[svc.service],
                description=svc.description,
                state=state,
                label=LABELS[state],
                uptime=uptime,
                bars=bars,
            )
        )

    found = [s.state for s in services]
    overall: Any
    if "major_outage" in found:
        overall, headline = "major_outage", "Major outage"
    elif "partial_outage" in found:
        overall, headline = "partial_outage", "Partial outage"
    elif "maintenance" in found:
        overall, headline = "maintenance", "Scheduled maintenance in progress"
    elif found and all(s == "unknown" for s in found):
        overall, headline = "unknown", "Checking systems"
    else:
        overall, headline = "operational", "All systems operational"

    incidents = list(notices)
    if page.show_incidents:
        incidents += _merge_detected(await storage.incidents(limit=500), public, since, now)
    incidents.sort(key=lambda i: i.started, reverse=True)
    active = [i for i in incidents if i.ongoing]
    history = [i for i in incidents if not i.ongoing][:MAX_INCIDENTS]
    return StatusView(
        title=page.title or f"{name} status",
        description=page.description,
        state=overall,
        headline=headline,
        services=services,
        active=active,
        history=history,
        days=page.days,
        updated=now.isoformat(),
    )
