from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Path]:
    """Write api-qovrix.yaml and monitor files into a temp folder and return its path."""

    def _make(config: str, files: dict[str, str] | None = None) -> Path:
        (tmp_path / "api-qovrix.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
        for name, content in (files or {}).items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(content), encoding="utf-8")
        return tmp_path

    return _make


def pytest_runtest_logreport(report):
    """In GitHub Actions, publish failing tests as annotations (readable on the run page and via the API)."""
    import os
    import sys

    if not report.failed or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    text = str(report.longrepr)
    lines = [line for line in text.splitlines() if line.strip()][-25:]
    message = "\n".join(lines)[-3000:]
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    title = report.nodeid.replace("%", "%25").replace(":", "%3A").replace(",", "%2C")
    sys.__stdout__.write(f"\n::error title={title}::{escaped}\n")
    sys.__stdout__.flush()
