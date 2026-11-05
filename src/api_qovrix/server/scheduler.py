"""The `serve` loop: run each monitor on its interval, store results, apply alert rules."""

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

from api_qovrix.config.loader import Project, ResolvedMonitor
from api_qovrix.engine.results import MonitorResult
from api_qovrix.engine.runner import RunContext, RunOptions, make_context, run_monitor
from api_qovrix.errors import ConfigError
from api_qovrix.server.alerting import AlertManager, Clock, utcnow
from api_qovrix.server.storage import Storage

FIRST_RUN_SPREAD = 15.0  # seconds: start monitors within this window so they don't all fire at once
PRUNE_EVERY = 3600.0
Log = Callable[[str], None]


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


class Server:
    """Owns the run context, storage and alert manager for one `serve` process."""

    def __init__(
        self,
        project: Project,
        storage: Storage,
        options: RunOptions | None = None,
        clock: Clock = utcnow,
        log: Log = print,
    ) -> None:
        unsupported = [m.name for m in project.monitors if m.monitor.cron]
        if unsupported:
            raise ConfigError(
                f"'cron' schedules are not supported yet; use 'interval' ({', '.join(unsupported)})"
            )
        self.project = project
        self.storage = storage
        self.options = options or RunOptions(concurrency=project.config.server.concurrency)
        self.clock = clock
        self.log = log
        self.alerts = AlertManager(project, storage, clock, self.options.transport, log)
        self.ctx: RunContext = make_context(project, self.options)
        self.semaphore = asyncio.Semaphore(max(1, self.options.concurrency))
        self._stop = asyncio.Event()

    async def check(self, m: ResolvedMonitor) -> MonitorResult:
        """Run one monitor, store the result and apply the alert rules."""
        started_at = self.clock().isoformat()
        async with self.semaphore:
            result = await run_monitor(m, self.ctx)
        await self.storage.record_run(result, self.project.environment, started_at)
        await self.alerts.process(m, result)
        self.log(describe(result))
        return result

    async def run_once(self) -> list[MonitorResult]:
        """Check every monitor once, then send alerts (used by `serve --once` and tests)."""
        await self.storage.forget_monitors({m.name for m in self.project.monitors})
        results = list(await asyncio.gather(*(self.check(m) for m in self.project.monitors)))
        await self.alerts.flush()
        return results

    async def _monitor_loop(self, m: ResolvedMonitor) -> None:
        await self._sleep(random.uniform(0, min(m.interval, FIRST_RUN_SPREAD)))  # noqa: S311 (not crypto)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.check(m)
            except Exception as exc:  # never let one monitor stop the scheduler
                self.log(f"✘ {m.name}: internal error: {type(exc).__name__}: {exc}")
            await self._sleep(max(0.0, m.interval - (time.monotonic() - started)))

    async def _flush_loop(self) -> None:
        wait = self.project.config.server.group_wait
        while not self._stop.is_set():
            await self._sleep(wait)
            await self.alerts.flush()

    async def _prune_loop(self) -> None:
        keep = self.project.config.server.retention
        while not self._stop.is_set():
            cutoff = (self.clock() - timedelta(seconds=keep)).isoformat()
            removed = await self.storage.prune(cutoff)
            if removed:
                self.log(f"pruned {removed} runs older than {cutoff}")
            await self._sleep(PRUNE_EVERY)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def stop(self) -> None:
        self._stop.set()

    async def serve(self) -> None:
        """Run until stop() is called; then send pending alerts and close everything."""
        await self.storage.forget_monitors({m.name for m in self.project.monitors})
        active = [m for m in self.project.monitors if m.monitor.enabled]
        self.log(
            f"api-qovrix serve: {len(active)} monitors"
            + (f" · env {self.project.environment}" if self.project.environment else "")
        )
        for m in self.project.monitors:
            if not m.monitor.enabled:
                await self.check(m)  # records the 'paused' state
        tasks = [asyncio.create_task(self._monitor_loop(m)) for m in active]
        tasks += [asyncio.create_task(self._flush_loop()), asyncio.create_task(self._prune_loop())]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.alerts.flush()
            await self.ctx.clients.aclose()
            self.log("stopped")
