"""Run monitors: steps in order, retries, value extraction, assertions, OpenAPI validation and drift."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import TypeAdapter, ValidationError

from api_qovrix.config.loader import Project, ResolvedMonitor
from api_qovrix.config.models import Auth, DriftConfig, Step
from api_qovrix.config.templating import make_redactor, render_value
from api_qovrix.drift.shape import compare, compile_ignore, make_sensitive_check
from api_qovrix.drift.store import BaselineStore
from api_qovrix.engine import assertions as asserts
from api_qovrix.engine.auth import TokenCache, apply_auth
from api_qovrix.engine.http import HttpClients, check_host, join_url, send
from api_qovrix.engine.jsonpaths import MISSING, query
from api_qovrix.engine.results import (
    SEVERITY_ORDER,
    AssertionResult,
    DriftChange,
    DriftReport,
    MonitorResult,
    RunSummary,
    StepResult,
)
from api_qovrix.errors import QovrixError
from api_qovrix.openapi.spec import OpenAPISpec

AUTH_ADAPTER: TypeAdapter[Auth] = TypeAdapter(Auth)
BaselineMode = Literal["check", "update", "replace", "learn"]


@dataclass
class RunOptions:
    concurrency: int = 10
    baseline_mode: BaselineMode = "check"
    retry_delay: float = 1.0
    transport: httpx.AsyncBaseTransport | None = None
    on_result: Callable[[MonitorResult], None] | None = None


@dataclass
class RunContext:
    project: Project
    options: RunOptions
    clients: HttpClients
    tokens: TokenCache = field(default_factory=TokenCache)
    spec: OpenAPISpec | None = None
    store: BaselineStore | None = None

    def redact(self, text: str) -> str:
        return make_redactor([*self.project.secrets.known_values(), *self.tokens.issued])(text)


def select_monitors(
    project: Project,
    names: Iterable[str] = (),
    services: Iterable[str] = (),
    tags: Iterable[str] = (),
) -> list[ResolvedMonitor]:
    names, services, tags = set(names), set(services), set(tags)
    for n in names:
        project.monitor(n)  # raises with a helpful message when unknown
    known_services = {m.service for m in project.monitors}
    unknown = services - known_services
    if unknown:
        raise QovrixError(
            f"unknown service(s): {', '.join(sorted(unknown))} "
            f"(known: {', '.join(sorted(known_services)) or 'none'})"
        )
    selected = []
    for m in project.monitors:
        if names and m.name not in names:
            continue
        if services and m.service not in services:
            continue
        if tags and not tags & set(m.tags):
            continue
        selected.append(m)
    return selected


def make_context(project: Project, options: RunOptions, load_spec: bool = True) -> RunContext:
    """Shared state for runs: HTTP clients, OAuth2 tokens, the OpenAPI spec and baselines.

    `serve` keeps one context alive for its whole lifetime; close it with `await ctx.clients.aclose()`.
    """
    ctx = RunContext(
        project=project,
        options=options,
        clients=HttpClients(options.transport),
        store=BaselineStore(project.baselines_dir),
    )
    if project.config.openapi and load_spec:
        ctx.spec = OpenAPISpec.load(project.config.openapi.source, project.root)
    return ctx


async def run_monitors(
    project: Project, monitors: list[ResolvedMonitor], options: RunOptions | None = None
) -> RunSummary:
    options = options or RunOptions()
    ctx = make_context(project, options, load_spec=any(m.validate_openapi for m in monitors))
    clients = ctx.clients

    semaphore = asyncio.Semaphore(max(1, options.concurrency))
    started = time.perf_counter()

    async def guarded(m: ResolvedMonitor) -> MonitorResult:
        async with semaphore:
            result = await run_monitor(m, ctx)
        if options.on_result:
            options.on_result(result)
        return result

    try:
        results = await asyncio.gather(*(guarded(m) for m in monitors))
    finally:
        await clients.aclose()
    return RunSummary(
        results=list(results),
        environment=project.environment,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def run_monitor(m: ResolvedMonitor, ctx: RunContext) -> MonitorResult:
    if not m.monitor.enabled:
        return MonitorResult(m.name, m.service, "skipped", tags=m.tags, warnings=["disabled"])

    started = time.perf_counter()
    attempts = 0
    steps: list[StepResult] = []
    while True:
        attempts += 1
        steps = await _run_steps(m, ctx)
        if all(s.passed or s.skipped for s in steps) or attempts > m.retries:
            break
        await asyncio.sleep(ctx.options.retry_delay * attempts)

    result = MonitorResult(m.name, m.service, "pass", steps=steps, attempts=attempts, tags=m.tags)
    if any(s.error for s in steps):
        result.status = "error"
    elif any(not s.passed and not s.skipped for s in steps):
        result.status = "fail"

    for step_cfg, step in zip(m.steps, steps, strict=False):
        drift_cfg = step_cfg.drift or m.drift
        if drift_cfg is None or step.skipped or step.error is not None or step.status_code is None:
            continue
        if step.status_code >= 400:
            continue  # error bodies have their own shape; status assertions report the failure
        if ctx.options.baseline_mode != "check" and not step.passed:
            result.warnings.append(f"{step.name}: baseline not updated because the step failed")
            continue
        step.drift = _drift(m, step_cfg, step, drift_cfg, ctx)
        if step.drift.failed and result.status == "pass":
            result.status = "fail"
        if step.drift.status == "no_baseline":
            result.warnings.append(
                f"{step.name}: no baseline yet. Run 'api-qovrix run {m.name} --update-baselines' and commit "
                ".api-qovrix/baselines/"
            )
        elif step.drift.status == "learning":
            result.warnings.append(
                f"{step.name}: baseline has {step.drift.samples}/{step.drift.min_samples} samples "
                "(changes are informational until then)"
            )

    for step in steps:
        if step.error:
            step.error = ctx.redact(step.error)
        step.url = ctx.redact(step.url)
        for a in step.assertions:
            a.message = ctx.redact(a.message)
            a.name = ctx.redact(a.name)

    result.duration_ms = (time.perf_counter() - started) * 1000
    return result


def _render_context(
    m: ResolvedMonitor, ctx: RunContext, extracted: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "secrets": ctx.project.secrets,
        "vars": m.vars,
        "steps": extracted,
        "env": {"name": ctx.project.environment},
    }


async def _run_steps(m: ResolvedMonitor, ctx: RunContext) -> list[StepResult]:
    results: list[StepResult] = []
    extracted: dict[str, dict[str, Any]] = {}
    failed = False
    for step in m.steps:
        if failed and not step.always_run:
            results.append(StepResult(step.name, step.request.method, step.request.url, skipped=True))
            continue
        result = await _run_step(m, step, ctx, extracted)
        results.append(result)
        if not result.passed:
            failed = True
    return results


async def _run_step(
    m: ResolvedMonitor, step: Step, ctx: RunContext, extracted: dict[str, dict[str, Any]]
) -> StepResult:
    req = step.request
    result = StepResult(step.name, req.method, req.url)
    render_ctx = _render_context(m, ctx, extracted)
    try:
        url = join_url(m.base_url, str(render_value(req.url, render_ctx)))
        result.url = url
        check_host(url, ctx.project.config.allowed_hosts)
        headers = {str(k): str(v) for k, v in render_value({**m.headers, **req.headers}, render_ctx).items()}
        params = dict(render_value(req.params, render_ctx))
        json_body = render_value(req.json_body, render_ctx)
        body = render_value(req.body, render_ctx)
        form = render_value(req.form, render_ctx)
        auth_cfg = req.auth or m.auth
        auth = None
        if auth_cfg is not None:
            auth = AUTH_ADAPTER.validate_python(render_value(auth_cfg.model_dump(by_alias=True), render_ctx))
        timeout = req.timeout or m.timeout
        client = ctx.clients.get(req.verify_tls)
        await apply_auth(auth, headers, params, ctx.tokens, client, timeout)
        if params:
            result.url = str(httpx.URL(url, params={k: str(v) for k, v in params.items()}))
        response = await send(
            client,
            req.method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            body=None if body is None else str(body),
            form=form,
            timeout=timeout,
            follow_redirects=req.follow_redirects,
        )
    except ValidationError as exc:
        result.error = f"invalid auth after rendering: {exc.errors()[0]['msg']}"
        return result
    except QovrixError as exc:
        result.error = str(exc)
        return result

    result.status_code = response.status_code
    result.duration_ms = round(response.elapsed_ms, 1)
    result.timings = response.timings
    result.headers = dict(response.headers)
    content_type = response.headers.get("content-type", "")
    parsed: Any = None
    is_json = False
    if response.content and ("json" in content_type or response.content.lstrip()[:1] in (b"{", b"[")):
        try:
            parsed = json.loads(response.content)
            is_json = True
        except ValueError:
            pass
    result.response_json = parsed
    result.is_json = is_json

    view = asserts.ResponseView(
        status_code=response.status_code,
        headers=response.headers,
        elapsed_ms=response.elapsed_ms,
        text=response.text,
        json=parsed,
        is_json=is_json,
    )
    try:
        for a in step.assertions:
            result.assertions.append(asserts.evaluate(a, view, ctx.project.root))
    except QovrixError as exc:
        result.error = str(exc)
        return result

    if not any(a.target == "status" for a in step.assertions) and response.status_code >= 400:
        result.assertions.append(AssertionResult("status < 400", False, f"got {response.status_code}"))

    if ctx.spec is not None and m.validate_openapi:
        ok, message = ctx.spec.validate(req.method, url, response.status_code, parsed, is_json)
        if message == "not in the OpenAPI spec":
            pass
        else:
            result.assertions.append(AssertionResult("openapi schema", ok, message))

    values: dict[str, Any] = {}
    for name, source in step.extract.items():
        if source.startswith("header:"):
            value: Any = response.headers.get(source.removeprefix("header:").strip(), MISSING)
        elif is_json:
            value = query(parsed, source)
        else:
            value = MISSING
        if value is MISSING:
            result.assertions.append(
                AssertionResult(f"extract {name}", False, f"{source} not found in the response")
            )
        else:
            values[name] = value
    extracted[step.name] = values
    return result


def _drift(
    m: ResolvedMonitor, step_cfg: Step, step: StepResult, cfg: DriftConfig, ctx: RunContext
) -> DriftReport:
    assert ctx.store is not None
    sensitive = make_sensitive_check(ctx.project.config.redact)
    entry = ctx.store.step(m.name, step.name)
    mode = ctx.options.baseline_mode

    if not step.is_json:
        if entry and entry["shape"].get("types"):
            report = DriftReport("changed", min_samples=cfg.min_samples, samples=entry["samples"])
            report.changes.append(DriftChange("$", "breaking", "response is no longer JSON"))
            report.failed = cfg.fail_on != "never"
            return report
        return DriftReport("ok", min_samples=cfg.min_samples)

    if entry is None and mode == "check":
        return DriftReport("no_baseline", min_samples=cfg.min_samples)

    changes = []
    trusted = False
    if entry is not None:
        trusted = bool(entry.get("accepted")) or entry["samples"] >= cfg.min_samples
        changes = compare(
            entry["shape"], step.response_json, ignored=compile_ignore(cfg.ignore), sensitive=sensitive
        )

    if mode in ("update", "replace", "learn"):
        new_entry = ctx.store.record(
            m.name,
            step.name,
            step.response_json,
            sensitive=sensitive,
            mode="replace" if mode == "replace" else "merge",
            accepted=None if mode == "learn" else True,
        )
        return DriftReport(
            "updated", changes=changes, samples=new_entry["samples"], min_samples=cfg.min_samples
        )

    assert entry is not None
    report = DriftReport(
        "changed" if changes else "ok",
        changes=changes,
        samples=entry["samples"],
        min_samples=cfg.min_samples,
    )
    if not trusted:
        report.status = "learning"
        for c in report.changes:
            c.severity = "info"
        return report
    threshold = {"breaking": SEVERITY_ORDER["breaking"], "warning": SEVERITY_ORDER["warning"]}.get(
        cfg.fail_on
    )
    if threshold is not None and any(SEVERITY_ORDER[c.severity] >= threshold for c in changes):
        report.failed = True
    return report
