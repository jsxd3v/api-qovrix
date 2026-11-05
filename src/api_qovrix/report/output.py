"""Run reports: terminal (grouped by service), JSON and JUnit XML."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from xml.etree import ElementTree as ET

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from api_qovrix.engine.results import DriftReport, MonitorResult, RunSummary

STATUS_STYLE = {
    "pass": ("✔", "green"),
    "fail": ("✘", "red"),
    "error": ("✘", "red"),
    "skipped": ("-", "dim"),
}
SEVERITY_STYLE = {"breaking": ("✘", "red"), "warning": ("!", "yellow"), "info": ("+", "cyan")}


def _status_code(r: MonitorResult) -> str:
    codes = [str(s.status_code) for s in r.steps if s.status_code is not None]
    if not codes:
        return "—"
    return codes[-1] if len(r.steps) == 1 else "/".join(codes)


def _duration(r: MonitorResult) -> str:
    total = sum(s.duration_ms or 0 for s in r.steps)
    return f"{total:.0f} ms" if r.steps and total else ""


def _drift_lines(step_name: str, multi: bool, drift: DriftReport) -> list[Text]:
    lines: list[Text] = []
    if not drift.changes:
        return lines
    accepted = drift.status == "updated"
    label = {
        "changed": "DRIFT",
        "learning": "DRIFT (learning)",
        "updated": "BASELINE UPDATED (changes accepted)",
    }.get(drift.status, "DRIFT")
    worst = drift.worst or "info"
    head = Text("    ")
    head.append(f"{label}", style="bold " + SEVERITY_STYLE[worst][1] if drift.status == "changed" else "bold")
    if drift.status == "changed":
        head.append(f" ({worst})", style=SEVERITY_STYLE[worst][1])
    if multi:
        head.append(f"  step {step_name}", style="dim")
    lines.append(head)
    width = max(len(c.path) for c in drift.changes)
    for c in drift.changes[:25]:
        sym, style = ("~", "dim") if accepted else SEVERITY_STYLE[c.severity]
        line = Text("      ")
        line.append(f"{sym} ", style=style)
        line.append(c.path.ljust(width + 2))
        line.append(c.message)
        if accepted or (c.severity != "breaking" and drift.status == "changed"):
            line.append(f"   {c.severity}", style="dim")
        lines.append(line)
    if len(drift.changes) > 25:
        lines.append(Text(f"      … {len(drift.changes) - 25} more", style="dim"))
    return lines


def print_summary(console: Console, summary: RunSummary, verbose: bool = False) -> None:
    groups: dict[str, list[MonitorResult]] = defaultdict(list)
    for r in summary.results:
        groups[r.service].append(r)

    for service in sorted(groups):
        console.print(Text(service, style="bold"))
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        table.add_column()
        extra: dict[str, list[Text]] = {}
        for r in sorted(groups[service], key=lambda x: x.name):
            sym, style = STATUS_STYLE[r.status]
            note = ""
            if r.status in ("fail", "error"):
                note = r.failure_summary
            elif r.status == "skipped":
                note = "disabled"
            if r.attempts > 1:
                note = f"{note} (attempt {r.attempts})".strip()
            table.add_row(
                Text(f"  {sym}", style=style),
                Text(r.name),
                Text(_status_code(r)),
                Text(_duration(r), style="dim"),
                Text(f"→ {note}" if note else "", style=style if r.status != "pass" else "dim"),
            )
            lines: list[Text] = []
            multi = len(r.steps) > 1
            for step in r.steps:
                if step.drift:
                    lines.extend(_drift_lines(step.name, multi, step.drift))
                if verbose:
                    lines.extend(_step_details(step))
            for w in r.warnings:
                if w != "disabled":
                    lines.append(Text(f"    ! {w}", style="yellow"))
            if lines:
                extra[r.name] = lines
        console.print(table)
        for name in sorted(extra):
            console.print(Text(f"  {name}", style="dim"))
            for line in extra[name]:
                console.print(line)
        console.print()

    parts = [f"{summary.count('pass')} passed"]
    if summary.count("fail"):
        parts.append(f"[red]{summary.count('fail')} failed[/red]")
    if summary.count("error"):
        parts.append(f"[red]{summary.count('error')} errors[/red]")
    if summary.count("skipped"):
        parts.append(f"{summary.count('skipped')} skipped")
    env = f" · env {escape(summary.environment)}" if summary.environment else ""
    console.print(", ".join(parts) + f" [dim]· {summary.duration_ms / 1000:.1f}s{env}[/dim]")


def _step_details(step: Any) -> list[Text]:
    lines = [Text(f"    {step.name}: {step.method} {step.url}", style="dim")]
    if step.timings:
        t = ", ".join(f"{k} {v:.0f} ms" for k, v in step.timings.items())
        lines.append(Text(f"      timings: {t}", style="dim"))
    for a in step.assertions:
        sym = "✔" if a.passed else "✘"
        style = "green" if a.passed else "red"
        line = Text(f"      {sym} ", style=style)
        line.append(a.name + (f": {a.message}" if a.message and not a.passed else ""))
        lines.append(line)
    return lines


def step_to_dict(s: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": s.name,
        "method": s.method,
        "url": s.url,
        "status_code": s.status_code,
        "duration_ms": s.duration_ms,
        "timings": s.timings,
        "skipped": s.skipped,
        "error": s.error,
        "assertions": [{"name": a.name, "passed": a.passed, "message": a.message} for a in s.assertions],
    }
    if s.drift:
        out["drift"] = {
            "status": s.drift.status,
            "failed": s.drift.failed,
            "samples": s.drift.samples,
            "changes": [
                {"path": c.path, "severity": c.severity, "message": c.message} for c in s.drift.changes
            ],
        }
    return out


def monitor_to_dict(r: MonitorResult) -> dict[str, Any]:
    """A monitor result without response bodies (safe to store and show)."""
    return {
        "name": r.name,
        "service": r.service,
        "status": r.status,
        "attempts": r.attempts,
        "duration_ms": round(r.duration_ms, 1),
        "tags": r.tags,
        "summary": r.failure_summary,
        "warnings": r.warnings,
        "steps": [step_to_dict(s) for s in r.steps],
    }


def summary_to_dict(summary: RunSummary) -> dict[str, Any]:
    return {
        "environment": summary.environment,
        "duration_ms": round(summary.duration_ms, 1),
        "ok": summary.ok,
        "counts": {
            "pass": summary.count("pass"),
            "fail": summary.count("fail"),
            "error": summary.count("error"),
            "skipped": summary.count("skipped"),
        },
        "monitors": [monitor_to_dict(r) for r in summary.results],
    }


def to_json(summary: RunSummary) -> str:
    return json.dumps(summary_to_dict(summary), indent=2, ensure_ascii=False)


def to_junit(summary: RunSummary) -> str:
    groups: dict[str, list[MonitorResult]] = defaultdict(list)
    for r in summary.results:
        groups[r.service].append(r)

    root = ET.Element(
        "testsuites",
        name="api-qovrix",
        tests=str(len(summary.results)),
        failures=str(summary.count("fail")),
        errors=str(summary.count("error")),
        skipped=str(summary.count("skipped")),
        time=f"{summary.duration_ms / 1000:.3f}",
    )
    for service in sorted(groups):
        results = groups[service]
        suite = ET.SubElement(
            root,
            "testsuite",
            name=service,
            tests=str(len(results)),
            failures=str(sum(r.status == "fail" for r in results)),
            errors=str(sum(r.status == "error" for r in results)),
            skipped=str(sum(r.status == "skipped" for r in results)),
            time=f"{sum(r.duration_ms for r in results) / 1000:.3f}",
        )
        for r in results:
            case = ET.SubElement(
                suite, "testcase", name=r.name, classname=service, time=f"{r.duration_ms / 1000:.3f}"
            )
            details = []
            for s in r.steps:
                details.append(f"[{s.name}] {s.method} {s.url} -> {s.status_code if s.status_code else '-'}")
                if s.error:
                    details.append(f"  error: {s.error}")
                for a in s.assertions:
                    details.append(
                        f"  {'PASS' if a.passed else 'FAIL'} {a.name}{': ' + a.message if a.message else ''}"
                    )
                if s.drift and s.drift.changes:
                    details.append(f"  drift ({s.drift.status}):")
                    details.extend(f"    {c.severity:<8} {c.path}  {c.message}" for c in s.drift.changes)
            details.extend(f"warning: {w}" for w in r.warnings)
            text = "\n".join(details)
            if r.status == "fail":
                ET.SubElement(case, "failure", message=r.failure_summary).text = text
            elif r.status == "error":
                ET.SubElement(case, "error", message=r.failure_summary).text = text
            elif r.status == "skipped":
                ET.SubElement(case, "skipped", message="disabled")
            else:
                ET.SubElement(case, "system-out").text = text
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
