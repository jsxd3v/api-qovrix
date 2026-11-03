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
