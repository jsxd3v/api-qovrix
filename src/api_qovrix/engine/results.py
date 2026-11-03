"""Result objects produced by a run. Reports (console, JSON, JUnit) are built from these."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["info", "warning", "breaking"]
SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "breaking": 2}
Status = Literal["pass", "fail", "error", "skipped"]


@dataclass
class AssertionResult:
    name: str
    passed: bool
    message: str = ""


@dataclass
class DriftChange:
    path: str
    severity: Severity
    message: str


@dataclass
class DriftReport:
    status: Literal["ok", "changed", "no_baseline", "learning", "updated"]
    changes: list[DriftChange] = field(default_factory=list)
    samples: int = 0
    min_samples: int = 0
    failed: bool = False

    @property
    def worst(self) -> Severity | None:
        if not self.changes:
            return None
        return max((c.severity for c in self.changes), key=lambda s: SEVERITY_ORDER[s])


@dataclass
class StepResult:
    name: str
    method: str
    url: str
    status_code: int | None = None
    duration_ms: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str | None = None
    skipped: bool = False
    drift: DriftReport | None = None
    response_json: Any = None
    is_json: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.skipped and self.error is None and all(a.passed for a in self.assertions)


@dataclass
class MonitorResult:
    name: str
    service: str
    status: Status
    steps: list[StepResult] = field(default_factory=list)
    attempts: int = 1
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def failure_summary(self) -> str:
        if self.error:
            return self.error
        for step in self.steps:
            if step.error:
                return f"{step.name}: {step.error}" if len(self.steps) > 1 else step.error
            failed = [a for a in step.assertions if not a.passed]
            if failed:
                msg = "; ".join(f"{a.name}: {a.message}" if a.message else a.name for a in failed)
                return f"{step.name}: {msg}" if len(self.steps) > 1 else msg
            if step.drift and step.drift.failed:
                return (
                    f"{step.name}: drift ({step.drift.worst})"
                    if len(self.steps) > 1
                    else f"drift ({step.drift.worst})"
                )
        return ""


@dataclass
class RunSummary:
    results: list[MonitorResult]
    environment: str | None
    duration_ms: float

    def count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def ok(self) -> bool:
        return all(r.status in ("pass", "skipped") for r in self.results)
