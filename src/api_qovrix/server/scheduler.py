"""The `serve` loop: run each monitor on its interval or cron schedule, store results, apply alert rules.

Also: reload monitor files when they change, ping a heartbeat URL while healthy, and prune old history.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from api_qovrix.config.loader import Project, ResolvedMonitor
from api_qovrix.engine.http import USER_AGENT
from api_qovrix.engine.results import MonitorResult
from api_qovrix.engine.runner import RunContext, RunOptions, make_context, run_monitor
from api_qovrix.errors import QovrixError
from api_qovrix.server.alerting import AlertManager, Clock, utcnow
from api_qovrix.server.cron import Cron, timezone
from api_qovrix.server.storage import Storage

FIRST_RUN_SPREAD = 15.0  # seconds: start monitors within this window so they don't all fire at once
PRUNE_EVERY = 3600.0
WATCH_EVERY = 2.0
Log = Callable[[str], None]
Reloader = Callable[[], Project]


def default_data_dir(project: Project) -> Path:
    return project.root / ".api-qovrix" / "data"


def prepare_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# run history: never commit\n*\n", encoding="utf-8")
    return path / "api-qovrix.db"


def make_logger(fmt: str = "text") -> Log:
    def log(message: str) -> None:
        ts = utcnow().isoformat().replace("+00:00", "Z")
        if fmt == "json":
            line = json.dumps({"time": ts, "message": message}, ensure_ascii=False)
        else:
            line = f"{ts}  {message}"
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    return log


def describe(result: MonitorResult) -> str:
    sym = {"pass": "✔", "fail": "✘", "error": "✘", "skipped": "-"}[result.status]
    codes = "/".join(str(s.status_code) for s in result.steps if s.status_code is not None) or "-"
    line = f"{sym} {result.name} [{result.service}] {codes} {result.duration_ms:.0f}ms"
    if result.status in ("fail", "error"):
        line += f" → {result.failure_summary}"
    return line


def signature(m: ResolvedMonitor) -> str:
    """Everything that changes how a monitor runs; used to restart only monitors that changed on reload."""
    data = {
        "monitor": m.monitor.model_dump(mode="json", by_alias=True),
        "service": m.service,
        "base_url": m.base_url,
        "auth": m.auth.model_dump(mode="json", by_alias=True) if m.auth else None,
        "headers": m.headers,
        "timeout": m.timeout,
        "retries": m.retries,
        "interval": m.interval,
        "vars": m.vars,
        "drift": m.drift.model_dump(mode="json") if m.drift else None,
        "openapi": m.validate_openapi,
    }
    return json.dumps(data, sort_keys=True, default=str)


def watched_files(project: Project) -> dict[str, tuple[int, int]]:
    """Config, monitor files and a local OpenAPI file, with (mtime, size): any change triggers a reload."""
    paths = {project.config_path}
    for pattern in project.config.include:
        paths.update(p for p in project.root.glob(pattern) if p.is_file() and p.suffix in (".yaml", ".yml"))
    spec = project.config.openapi.source if project.config.openapi else None
    if spec and not spec.startswith(("http://", "https://")):
        paths.add(project.root / spec)
    out: dict[str, tuple[int, int]] = {}
    for p in paths:
        with contextlib.suppress(OSError):
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


class Server:
    """Owns the run context, storage and alert manager for one `serve` process."""

    def __init__(
        self,
        project: Project,
        storage: Storage,
        options: RunOptions | None = None,
        clock: Clock = utcnow,
        log: Log = print,
        reloader: Reloader | None = None,
    ) -> None:
        self.project = project
        self.storage = storage
        self.options = options or RunOptions(concurrency=project.config.server.concurrency)
        self.clock = clock
        self.log = log
        self.reloader = reloader
        self.alerts = AlertManager(project, storage, clock, self.options.transport, log)
        self.ctx: RunContext = make_context(project, self.options)
        self.semaphore = asyncio.Semaphore(max(1, self.options.concurrency))
        self._stop = asyncio.Event()
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._signatures: dict[str, str] = {}
        self.last_check_at: str | None = None
        self.started_at: str | None = None

    # ------------------------------------------------------------------ checks

    async def check(self, m: ResolvedMonitor) -> MonitorResult:
        """Run one monitor, store the result and apply the alert rules."""
        started_at = self.clock().isoformat()
        async with self.semaphore:
            result = await run_monitor(m, self.ctx)
        await self.storage.record_run(result, self.project.environment, started_at)
        await self.alerts.process(m, result)
        self.last_check_at = self.clock().isoformat()
        self.log(describe(result))
        return result

    async def run_once(self) -> list[MonitorResult]:
        """Check every monitor once, then send alerts (used by `serve --once` and tests)."""
        await self.storage.forget_monitors({m.name for m in self.project.monitors})
        results = list(await asyncio.gather(*(self.check(m) for m in self.project.monitors)))
        await self.alerts.flush()
        return results

    def schedule(self, m: ResolvedMonitor) -> str:
        return f"cron {m.monitor.cron}" if m.monitor.cron else f"every {m.interval:g}s"

    # ------------------------------------------------------------------ loops

    async def _monitor_loop(self, m: ResolvedMonitor) -> None:
        cron = Cron.parse(m.monitor.cron) if m.monitor.cron else None
        if cron is None:
            await self._sleep(random.uniform(0, min(m.interval, FIRST_RUN_SPREAD)))  # noqa: S311 (not crypto)
        while not self._stop.is_set():
            if cron is not None:
                now = self.clock()
                tz = timezone(self.project.config.server.timezone)
                await self._sleep(max(0.0, (cron.next_after(now, tz) - now).total_seconds()))
                if self._stop.is_set():
                    break
            started = time.monotonic()
            try:
                await self.check(m)
            except Exception as exc:  # never let one monitor stop the scheduler
                self.log(f"✘ {m.name}: internal error: {type(exc).__name__}: {exc}")
            if cron is None:
                await self._sleep(max(0.0, m.interval - (time.monotonic() - started)))

    async def _flush_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.project.config.server.group_wait)
            await self.alerts.flush()

    async def _prune_loop(self) -> None:
        while not self._stop.is_set():
            cutoff = (self.clock() - timedelta(seconds=self.project.config.server.retention)).isoformat()
            removed = await self.storage.prune(cutoff)
            if removed:
                self.log(f"pruned {removed} runs older than {cutoff}")
            await self._sleep(PRUNE_EVERY)

    async def _heartbeat_loop(self) -> None:
        failing = False
        while not self._stop.is_set():
            hb = self.project.config.server.heartbeat
            if hb is None or not hb.url:
                await self._sleep(30.0)
                continue
            if self.health()["status"] == "ok":
                ok, detail = await self.ping(hb.url, hb.method, hb.timeout)
                if not ok:
                    self.log(f"! heartbeat failed: {detail}")
                elif failing:
                    self.log("heartbeat ok again")
                failing = not ok
            await self._sleep(hb.interval)

    async def ping(self, url: str, method: str, timeout: float) -> tuple[bool, str]:
        kwargs: dict[str, Any] = {"timeout": timeout, "headers": {"User-Agent": USER_AGENT}}
        if self.options.transport is not None:
            kwargs["transport"] = self.options.transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                resp = await client.request(method, url)
        except httpx.HTTPError as exc:
            return False, type(exc).__name__  # never log the URL: it usually contains a token
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}"
        return True, ""

    async def _watch_loop(self) -> None:
        seen = watched_files(self.project)
        while not self._stop.is_set():
            await self._sleep(WATCH_EVERY)
            if self._stop.is_set() or not self.project.config.server.watch:
                continue
            now = watched_files(self.project)
            if now != seen:
                seen = now
                await self.reload()

    # ------------------------------------------------------------------ reload

    async def reload(self) -> bool:
        """Load the config again and restart only the monitors that changed. Keeps running on errors."""
        if self.reloader is None:
            return False
        try:
            new = self.reloader()
            new_ctx = make_context(new, self.options)
        except QovrixError as exc:
            self.log(f"! reload failed, keeping the previous monitors: {exc}")
            return False
        new_ctx.tokens = self.ctx.tokens  # keep logins that are still valid
        old_ctx, self.ctx = self.ctx, new_ctx
        await old_ctx.clients.aclose()
        self.project = new
        self.alerts.project = new

        new_sigs = {m.name: signature(m) for m in new.monitors}
        added = [n for n in new_sigs if n not in self._signatures]
        removed = [n for n in self._signatures if n not in new_sigs]
        changed = [n for n in new_sigs if n in self._signatures and new_sigs[n] != self._signatures[n]]
        for name in removed + changed:
            task = self._loops.pop(name, None)
            if task:
                task.cancel()
        await self.storage.forget_monitors(set(new_sigs))
        for m in new.monitors:
            if m.name in added or m.name in changed:
                self._start(m)
        self._signatures = new_sigs
        parts = [f"+{len(added)} added", f"-{len(removed)} removed", f"~{len(changed)} changed"]
        self.log(f"reloaded monitors ({', '.join(parts)})")
        return True

    def _start(self, m: ResolvedMonitor) -> None:
        if m.monitor.enabled:
            self._loops[m.name] = asyncio.create_task(self._monitor_loop(m))
        else:
            self._loops[m.name] = asyncio.create_task(self._record_paused(m))

    async def _record_paused(self, m: ResolvedMonitor) -> None:
        await self.check(m)  # stores the 'paused' state; nothing to schedule

    # ------------------------------------------------------------------ lifecycle

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def health(self) -> dict[str, Any]:
        crashed = [n for n, t in self._loops.items() if t.done() and not t.cancelled() and t.exception()]
        status = "ok" if self.started_at and not self.stopping and not crashed else "unavailable"
        return {
            "status": status,
            "started_at": self.started_at,
            "last_check_at": self.last_check_at,
            "monitors": len(self.project.monitors),
        }

    async def serve(self) -> None:
        """Run until stop() is called; then send pending alerts and close everything."""
        await self.storage.forget_monitors({m.name for m in self.project.monitors})
        active = [m for m in self.project.monitors if m.monitor.enabled]
        self.log(
            f"api-qovrix serve: {len(active)} monitors"
            + (f" · env {self.project.environment}" if self.project.environment else "")
            + (
                f" · cron in {self.project.config.server.timezone}"
                if any(m.monitor.cron for m in active)
                else ""
            )
        )
        self.started_at = self.clock().isoformat()
        self._signatures = {m.name: signature(m) for m in self.project.monitors}
        for m in self.project.monitors:
            self._start(m)
        background = [
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._prune_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]
        if self.reloader is not None:
            background.append(asyncio.create_task(self._watch_loop()))
        try:
            await self._stop.wait()
        finally:
            tasks = [*self._loops.values(), *background]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.alerts.flush()
            await self.ctx.clients.aclose()
            self.log("stopped")
