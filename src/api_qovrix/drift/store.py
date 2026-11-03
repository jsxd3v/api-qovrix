"""Baselines stored as JSON files in the repo: .api-qovrix/baselines/<monitor>.json

Files are written with sorted keys and a trailing newline so changes show up as clean Git diffs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from api_qovrix.drift.shape import Node, merge, new_node

FORMAT_VERSION = 1


class BaselineStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, monitor: str) -> Path:
        return self.directory / f"{monitor}.json"

    def load(self, monitor: str) -> dict[str, Any] | None:
        p = self.path(monitor)
        if not p.is_file():
            return None
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data

    def step(self, monitor: str, step: str) -> dict[str, Any] | None:
        data = self.load(monitor)
        if not data:
            return None
        result: dict[str, Any] | None = data.get("steps", {}).get(step)
        return result

    def save(self, monitor: str, data: dict[str, Any]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        p = self.path(monitor)
        p.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    def record(
        self,
        monitor: str,
        step: str,
        value: Any,
        *,
        sensitive: Any,
        mode: Literal["merge", "replace"] = "merge",
        accepted: bool | None = None,
    ) -> dict[str, Any]:
        """Add a sample (merge) or start a new baseline from it (replace). Returns the step entry."""
        data = self.load(monitor) or {"monitor": monitor, "version": FORMAT_VERSION, "steps": {}}
        entry = data["steps"].get(step)
        if entry is None or mode == "replace":
            entry = {"samples": 0, "accepted": False, "shape": new_node()}
        shape: Node = entry["shape"]
        merge(shape, value, sensitive=sensitive)
        entry["samples"] += 1
        if accepted is not None:
            entry["accepted"] = accepted
        entry["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        data["steps"][step] = entry
        self.save(monitor, data)
        result: dict[str, Any] = entry
        return result

    def delete(self, monitor: str) -> bool:
        p = self.path(monitor)
        if p.is_file():
            p.unlink()
            return True
        return False
