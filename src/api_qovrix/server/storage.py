"""SQLite storage for `serve`: run history, current monitor state and incidents.

Uses Python's built-in sqlite3 (no extra dependency). All calls go through one connection guarded by a lock
and run in a worker thread, so the event loop never blocks on disk I/O. WAL mode lets the dashboard read
while the scheduler writes.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

from api_qovrix.engine.results import MonitorResult
from api_qovrix.report.output import monitor_to_dict

SCHEMA_VERSION = 2
DAILY_KEEP_DAYS = 400  # daily summaries outlive raw runs, for the 90-day status page
RESOLVE_SQL = "UPDATE incidents SET resolved_at = ? WHERE monitor = ? AND resolved_at IS NULL"
T = TypeVar("T")
State = Literal["unknown", "up", "down", "paused"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor     TEXT NOT NULL,
    service     TEXT NOT NULL,
    environment TEXT,
    started_at  TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    details     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_monitor_time ON runs (monitor, started_at);
CREATE INDEX IF NOT EXISTS runs_time ON runs (started_at);

CREATE TABLE IF NOT EXISTS monitor_state (
    monitor              TEXT PRIMARY KEY,
    service              TEXT NOT NULL,
    state                TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    since                TEXT,
    last_run_at          TEXT,
    last_status          TEXT,
    last_summary         TEXT,
    last_duration_ms     REAL,
    last_alert_at        TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor     TEXT NOT NULL,
    service     TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    resolved_at TEXT,
    summary     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents (monitor, resolved_at);

CREATE TABLE IF NOT EXISTS daily (
    monitor TEXT NOT NULL,
    service TEXT NOT NULL,
    day     TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    total   INTEGER NOT NULL,
    PRIMARY KEY (monitor, day)
);
CREATE INDEX IF NOT EXISTS daily_service ON daily (service, day);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class MonitorState:
    monitor: str
    service: str
    state: State = "unknown"
    consecutive_failures: int = 0
    since: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_summary: str | None = None
    last_duration_ms: float | None = None
    last_alert_at: str | None = None


@dataclass
class Incident:
    id: int
    monitor: str
    service: str
    opened_at: str
    resolved_at: str | None
    summary: str


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{path} was created by a newer api-qovrix (schema {row['value']}); upgrade api-qovrix"
                )
            elif int(row["value"]) < 2:
                # v1 -> v2: build the daily summaries from the run history that exists
                self._conn.execute(
                    "INSERT OR REPLACE INTO daily (monitor, service, day, ok, total) "
                    "SELECT monitor, service, substr(started_at, 1, 10), SUM(status = 'pass'), COUNT(*) "
                    "FROM runs WHERE status != 'skipped' GROUP BY monitor, substr(started_at, 1, 10)"
                )
                self._conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
                )

    # ------------------------------------------------------------------ sync core

    def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            return fn(self._conn)

    async def _arun(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._run, fn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ runs

    async def record_run(self, result: MonitorResult, environment: str | None, started_at: str) -> int:
        details = json.dumps(monitor_to_dict(result), ensure_ascii=False)

        def q(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO runs (monitor, service, environment, started_at, duration_ms, status, summary, "
                "details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.name,
                    result.service,
                    environment,
                    started_at,
                    round(result.duration_ms, 1),
                    result.status,
                    result.failure_summary,
                    details,
                ),
            )
            if result.status != "skipped":
                c.execute(
                    "INSERT INTO daily (monitor, service, day, ok, total) VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT (monitor, day) DO UPDATE SET ok = ok + excluded.ok, total = total + 1, "
                    "service = excluded.service",
                    (result.name, result.service, started_at[:10], 1 if result.status == "pass" else 0),
                )
            return int(cur.lastrowid or 0)

        return await self._arun(q)

    async def history(self, monitor: str, limit: int = 50) -> list[dict[str, Any]]:
        def q(c: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = c.execute(
                "SELECT id, started_at, duration_ms, status, summary FROM runs WHERE monitor = ? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (monitor, limit),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._arun(q)

    async def run_details(self, run_id: int) -> dict[str, Any] | None:
        def q(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute("SELECT details FROM runs WHERE id = ?", (run_id,)).fetchone()
            return json.loads(row["details"]) if row else None

        return await self._arun(q)

    async def uptime(self, since: str) -> dict[str, tuple[int, int]]:
        """Per monitor: (passed runs, counted runs) since a time. Skipped runs are not counted."""

        def q(c: sqlite3.Connection) -> dict[str, tuple[int, int]]:
            rows = c.execute(
                "SELECT monitor, SUM(status = 'pass') AS ok, COUNT(*) AS total FROM runs "
                "WHERE started_at >= ? AND status != 'skipped' GROUP BY monitor",
                (since,),
            ).fetchall()
            return {r["monitor"]: (int(r["ok"] or 0), int(r["total"])) for r in rows}

        return await self._arun(q)

    async def daily(self, since_day: str, monitors: list[str] | None = None) -> list[dict[str, Any]]:
        """Per monitor and UTC day: passed and counted checks (skipped runs are not counted)."""

        def q(c: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = c.execute(
                "SELECT monitor, service, day, ok, total FROM daily WHERE day >= ? ORDER BY day", (since_day,)
            ).fetchall()
            out = [dict(r) for r in rows]
            return [r for r in out if monitors is None or r["monitor"] in monitors]

        return await self._arun(q)

    async def hourly(self, since: str) -> dict[str, dict[str, tuple[int, int]]]:
        """Per monitor and UTC hour ('YYYY-MM-DDTHH'): (passed, counted) since a time."""

        def q(c: sqlite3.Connection) -> dict[str, dict[str, tuple[int, int]]]:
            rows = c.execute(
                "SELECT monitor, substr(started_at, 1, 13) AS hour, SUM(status = 'pass') AS ok, "
                "COUNT(*) AS total FROM runs WHERE started_at >= ? AND status != 'skipped' "
                "GROUP BY monitor, hour",
                (since,),
            ).fetchall()
            out: dict[str, dict[str, tuple[int, int]]] = {}
            for r in rows:
                out.setdefault(r["monitor"], {})[r["hour"]] = (int(r["ok"] or 0), int(r["total"]))
            return out

        return await self._arun(q)

    async def prune(self, older_than: str) -> int:
        def q(c: sqlite3.Connection) -> int:
            runs = c.execute("DELETE FROM runs WHERE started_at < ?", (older_than,)).rowcount
            keep_from = (
                (datetime.fromisoformat(older_than) - timedelta(days=DAILY_KEEP_DAYS)).date().isoformat()
            )
            c.execute("DELETE FROM daily WHERE day < ?", (keep_from,))
            c.execute(
                "DELETE FROM incidents WHERE resolved_at IS NOT NULL AND resolved_at < ?", (older_than,)
            )
            return int(runs)

        return await self._arun(q)

    # ------------------------------------------------------------------ state

    async def get_state(self, monitor: str) -> MonitorState | None:
        def q(c: sqlite3.Connection) -> MonitorState | None:
            row = c.execute("SELECT * FROM monitor_state WHERE monitor = ?", (monitor,)).fetchone()
            return MonitorState(**dict(row)) if row else None

        return await self._arun(q)

    async def save_state(self, state: MonitorState) -> None:
        data = asdict(state)
        cols = [f.name for f in fields(MonitorState)]

        def q(c: sqlite3.Connection) -> None:
            c.execute(
                f"INSERT OR REPLACE INTO monitor_state ({', '.join(cols)}) "  # noqa: S608 (fixed column list)
                f"VALUES ({', '.join('?' for _ in cols)})",
                [data[k] for k in cols],
            )

        await self._arun(q)

    async def all_states(self) -> list[MonitorState]:
        def q(c: sqlite3.Connection) -> list[MonitorState]:
            rows = c.execute("SELECT * FROM monitor_state ORDER BY service, monitor").fetchall()
            return [MonitorState(**dict(r)) for r in rows]

        return await self._arun(q)

    async def forget_monitors(self, keep: set[str]) -> int:
        """Remove state for monitors that no longer exist in the config (history is kept)."""

        def q(c: sqlite3.Connection) -> int:
            names = [r["monitor"] for r in c.execute("SELECT monitor FROM monitor_state").fetchall()]
            gone = [n for n in names if n not in keep]
            for n in gone:
                c.execute("DELETE FROM monitor_state WHERE monitor = ?", (n,))
                c.execute(RESOLVE_SQL, (now_iso(), n))
            return len(gone)

        return await self._arun(q)

    # ------------------------------------------------------------------ incidents

    async def open_incident(self, monitor: str, service: str, opened_at: str, summary: str) -> int:
        def q(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO incidents (monitor, service, opened_at, summary) VALUES (?, ?, ?, ?)",
                (monitor, service, opened_at, summary),
            )
            return int(cur.lastrowid or 0)

        return await self._arun(q)

    async def resolve_incident(self, monitor: str, resolved_at: str) -> None:
        def q(c: sqlite3.Connection) -> None:
            c.execute(RESOLVE_SQL, (resolved_at, monitor))

        await self._arun(q)

    async def incidents(self, open_only: bool = False, limit: int = 100) -> list[Incident]:
        def q(c: sqlite3.Connection) -> list[Incident]:
            where = "WHERE resolved_at IS NULL" if open_only else ""
            rows = c.execute(
                f"SELECT * FROM incidents {where} ORDER BY opened_at DESC, id DESC LIMIT ?",  # noqa: S608
                (limit,),
            ).fetchall()
            return [Incident(**dict(r)) for r in rows]

        return await self._arun(q)
