"""Small, comment-preserving edits to api-qovrix.yaml and writing generated monitor files."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from api_qovrix.errors import ConfigError


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


def _load(path: Path) -> CommentedMap:
    data = _yaml().load(path.read_text(encoding="utf-8")) or CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _save(path: Path, data: CommentedMap) -> None:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def add_secrets(config_path: Path, secrets: dict[str, str]) -> list[str]:
    """Add `NAME: ${env:NAME}` entries that are missing. Returns the names added."""
    data = _load(config_path)
    section = data.get("secrets")
    if section is None:
        section = CommentedMap()
        data["secrets"] = section
    elif not isinstance(section, CommentedMap):
        raise ConfigError(f"{config_path}: 'secrets' must be a mapping of NAME: ${{env:VARIABLE}}")
    added = []
    for name, description in secrets.items():
        if name in section:
            continue
        section[name] = f"${{env:{name}}}"
        if description:
            section.yaml_add_eol_comment(description, name)
        added.append(name)
    if added:
        _save(config_path, data)
    return added


def set_openapi_source(config_path: Path, source: str) -> bool:
    data = _load(config_path)
    if data.get("openapi"):
        return False
    data["openapi"] = CommentedMap(source=source, validate_responses=True)
    _save(config_path, data)
    return True


def dump_yaml(data: Any, header: str = "") -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    text = buf.getvalue()
    if header:
        text = "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines()) + "\n" + text
    return text
