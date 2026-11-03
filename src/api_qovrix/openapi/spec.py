"""Load an OpenAPI 3.0 / 3.1 document, match requests to operations and validate response bodies."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from jsonschema import Draft4Validator, Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4, DRAFT202012

from api_qovrix.engine.assertions import schema_errors
from api_qovrix.errors import ConfigError

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SPEC_URI = "urn:api-qovrix:openapi"


@dataclass
class Operation:
    method: str
    path: str
    operation_id: str | None
    raw: dict[str, Any]
    path_item: dict[str, Any]


def load_document(source: str, root: Path | None = None) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        try:
            resp = httpx.get(source, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConfigError(f"could not download OpenAPI document {source}: {exc}") from None
        text = resp.text
    else:
        path = (root / source) if root else Path(source)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise ConfigError(f"OpenAPI document not found: {path}") from None
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"OpenAPI document {source} is not valid JSON/YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"OpenAPI document {source} must be an object")
    if "swagger" in data:
        raise ConfigError("Swagger 2.0 is not supported; convert it to OpenAPI 3 first")
    version = str(data.get("openapi", ""))
    if not version.startswith("3."):
        raise ConfigError(f"{source} is not an OpenAPI 3.x document (openapi: {version or 'missing'})")
    return data


def _convert_30(node: Any) -> Any:
    """OpenAPI 3.0 schema objects -> JSON Schema draft 4 (mainly `nullable`)."""
    if isinstance(node, list):
        return [_convert_30(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _convert_30(v) for k, v in node.items()}
    if out.get("nullable") is False:
        del out["nullable"]
    if out.get("nullable") is True:
        del out["nullable"]
        t = out.get("type")
        if isinstance(t, str):
            out["type"] = [t, "null"]
            if "enum" in out and None not in out["enum"]:
                out["enum"] = [*out["enum"], None]
        elif "$ref" in out or "allOf" in out or "oneOf" in out or "anyOf" in out:
            out = {"anyOf": [out, {"type": "null"}]}
    return out


def _pointer(*parts: str) -> str:
    return "#/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)


def _path_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}/]+\})", template.rstrip("/"))
    rx = "".join("[^/]+" if p.startswith("{") and p.endswith("}") else re.escape(p) for p in parts)
    return re.compile(rf"(?:^|.*?){rx}/?$")


class OpenAPISpec:
    def __init__(self, document: dict[str, Any], source: str = "openapi") -> None:
        self.source = source
        self.version = str(document.get("openapi", "3.0"))
        self.is_31 = self.version.startswith("3.1")
        self.raw = document
        self.document = document if self.is_31 else _convert_30(copy.deepcopy(document))
        spec = DRAFT202012 if self.is_31 else DRAFT4
        resource: Resource[Any] = spec.create_resource(self.document)
        self.registry: Registry[Any] = Registry().with_resource(SPEC_URI, resource)
        self._validators: dict[str, Any] = {}

    @classmethod
    def load(cls, source: str, root: Path | None = None) -> OpenAPISpec:
        return cls(load_document(source, root), source)

    @property
    def title(self) -> str:
        return str(self.raw.get("info", {}).get("title") or "api")

    @cached_property
    def servers(self) -> list[str]:
        return [str(s.get("url", "")) for s in self.raw.get("servers", []) if isinstance(s, dict)]

    def operations(self) -> list[Operation]:
        ops = []
        for path, item in (self.raw.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            item = self.resolve(item)
            for method in METHODS:
                op = item.get(method)
                if isinstance(op, dict):
                    ops.append(Operation(method, path, op.get("operationId"), op, item))
        return ops

    def resolve(self, node: Any, depth: int = 0) -> Any:
        """Follow a local `$ref` (for path items, responses, parameters, examples)."""
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 20:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise ConfigError(f"external $ref is not supported: {ref}")
            target: Any = self.raw
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or part not in target:
                    raise ConfigError(f"unresolvable $ref: {ref}")
                target = target[part]
            node = target
            depth += 1
        return node

    def find_operation(self, method: str, url: str) -> Operation | None:
        path = urlsplit(url).path or "/"
        method = method.lower()
        best: tuple[int, Operation] | None = None
        for op in self.operations():
            if op.method != method:
                continue
            if _path_regex(op.path).match(path.rstrip("/") or "/"):
                literal = len(re.sub(r"\{[^}]+\}", "", op.path))
                if best is None or literal > best[0]:
                    best = (literal, op)
        return best[1] if best else None

    def response_schema_pointer(self, op: Operation, status: int) -> tuple[str | None, str]:
        """Pointer to the JSON schema for this status, and a note. None when nothing to validate."""
        responses = op.raw.get("responses") or {}
        key = None
        for candidate in (str(status), f"{str(status)[0]}XX", f"{str(status)[0]}xx", "default"):
            if candidate in responses:
                key = candidate
                break
        if key is None:
            return None, f"status {status} is not documented for {op.method.upper()} {op.path}"
        response_ref = ("paths", op.path, op.method, "responses", key)
        response = responses[key]
        if isinstance(response, dict) and "$ref" in response:
            ref = response["$ref"]
            response = self.resolve(response)
            response_ref = tuple(p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/"))
        content = (response or {}).get("content") or {}
        media = next(
            (m for m in content if m.split(";")[0].strip() == "application/json"),
            next((m for m in content if m.split(";")[0].strip().endswith("+json")), None),
        )
        if media is None and "*/*" in content:
            media = "*/*"
        if media is None or "schema" not in (content.get(media) or {}):
            return None, ""
        return _pointer(*response_ref, "content", media, "schema"), ""

    def validate(self, method: str, url: str, status: int, body: Any, is_json: bool) -> tuple[bool, str]:
        """(passed, message). Unknown operations are not a failure (reported as a note by the runner)."""
        op = self.find_operation(method, url)
        if op is None:
            return True, "not in the OpenAPI spec"
        pointer, note = self.response_schema_pointer(op, status)
        if pointer is None:
            if note:
                return False, note
            return True, ""
        if not is_json:
            return False, "response is not JSON but the spec documents a JSON body"
        validator = self._validators.get(pointer)
        if validator is None:
            cls = Draft202012Validator if self.is_31 else Draft4Validator
            validator = cls({"$ref": SPEC_URI + pointer}, registry=self.registry)
            self._validators[pointer] = validator
        try:
            errors = schema_errors(validator, body)
        except Exception as exc:  # broken specs (bad $ref etc.) should not crash the run
            return False, f"could not validate against the spec: {exc}"
        if errors:
            return False, f"{op.method.upper()} {op.path} {status}: " + "; ".join(errors)
        return True, ""
