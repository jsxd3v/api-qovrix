"""`api-qovrix init` files and the published JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from api_qovrix.config.models import MonitorFile, ProjectConfig
from api_qovrix.errors import ConfigError

SCHEMA_BASE = "https://raw.githubusercontent.com/jsxd3v/api-qovrix/main/schema"

CONFIG_TEMPLATE = f"""\
# yaml-language-server: $schema={SCHEMA_BASE}/api-qovrix.schema.json
# api-qovrix project config. Docs: https://github.com/jsxd3v/api-qovrix
version: 1
project: {{project}}

defaults:
  interval: 1m
  timeout: 10s
  retries: 1

# Environments pick the base_url for relative monitor URLs: api-qovrix run --env staging
environments:
  production:
    base_url: https://jsonplaceholder.typicode.com

# Secrets always come from environment variables, never from this file.
secrets: {{{{}}}}
#   API_TOKEN: ${{{{env:MY_API_TOKEN}}}}

# Validate responses against your OpenAPI spec:
# openapi:
#   source: ./openapi.yaml

include:
  - monitors/**/*.yaml
"""

EXAMPLE_MONITORS = f"""\
# yaml-language-server: $schema={SCHEMA_BASE}/monitors.schema.json
# Example monitors against a free public test API. Replace them with your own.
monitors:
  - name: example-todo
    request:
      method: GET
      url: /todos/1
    assert:
      - status: 200
      - response_time_ms: {{ lt: 2000 }}
      - jsonpath: $.completed
        type: boolean
    drift:
      enabled: true

  - name: example-post-flow
    description: Create a post, then read the comments of an existing one
    steps:
      - name: create
        request:
          method: POST
          url: /posts
          json: {{ title: hello, body: from api-qovrix, userId: 1 }}
        assert:
          - status: 201
        extract:
          post_id: $.id
      - name: comments
        request:
          method: GET
          url: /posts/1/comments
        assert:
          - status: 200
          - jsonpath: $[*].email
            not_empty: true
"""


def init_project(directory: Path, project: str | None = None, force: bool = False) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "api-qovrix.yaml"
    example = directory / "monitors" / "example.yaml"
    for p in (config, example):
        if p.exists() and not force:
            raise ConfigError(f"{p} already exists (use --force to overwrite)")
    example.parent.mkdir(parents=True, exist_ok=True)
    name = project or directory.resolve().name or "my-api"
    config.write_text(CONFIG_TEMPLATE.format(project=json.dumps(name)), encoding="utf-8")
    example.write_text(EXAMPLE_MONITORS, encoding="utf-8")
    baselines = directory / ".api-qovrix" / "baselines"
    baselines.mkdir(parents=True, exist_ok=True)
    readme = directory / ".api-qovrix" / "README.md"
    if not readme.exists():
        readme.write_text(
            "# .api-qovrix\n\n"
            "`baselines/` holds the learned response shapes used for drift detection.\n"
            "Commit them, and review changes in pull requests like snapshot tests.\n\n"
            "Update them with `api-qovrix run --update-baselines` or `api-qovrix drift accept <monitor>`.\n",
            encoding="utf-8",
        )
    return [config, example, readme]


def json_schemas() -> dict[str, dict[str, Any]]:
    project = ProjectConfig.model_json_schema(by_alias=True)
    project["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    project["$id"] = f"{SCHEMA_BASE}/api-qovrix.schema.json"
    project["title"] = "api-qovrix project config (api-qovrix.yaml)"
    monitors = MonitorFile.model_json_schema(by_alias=True)
    monitors["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    monitors["$id"] = f"{SCHEMA_BASE}/monitors.schema.json"
    monitors["title"] = "api-qovrix monitor file (monitors/*.yaml)"
    return {"api-qovrix.schema.json": project, "monitors.schema.json": monitors}


def write_schemas(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, schema in json_schemas().items():
        p = directory / name
        p.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths.append(p)
    return paths
