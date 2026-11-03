"""Generate monitors from an OpenAPI document.

Only safe methods (GET, HEAD) are generated. Monitors whose required parameters have no example are
written with `enabled: false` and a CHANGE_ME value, so they never call a wrong URL by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from api_qovrix.openapi.spec import OpenAPISpec, Operation

SAFE_METHODS = ("get", "head")
PLACEHOLDER = "CHANGE_ME"


@dataclass
class ImportResult:
    service: str
    service_block: dict[str, Any]
    monitors: list[dict[str, Any]]
    secrets: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    skipped_unsafe: int = 0


def slug(text: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "api"


def _example(spec: OpenAPISpec, param: dict[str, Any]) -> tuple[bool, Any]:
    if "example" in param:
        return True, param["example"]
    examples = param.get("examples")
    if isinstance(examples, dict) and examples:
        first = spec.resolve(next(iter(examples.values())))
        if isinstance(first, dict) and "value" in first:
            return True, first["value"]
    schema = spec.resolve(param.get("schema") or {})
    if isinstance(schema, dict):
        for key in ("example", "default"):
            if key in schema:
                return True, schema[key]
        if isinstance(schema.get("examples"), list) and schema["examples"]:
            return True, schema["examples"][0]
        if isinstance(schema.get("enum"), list) and schema["enum"]:
            return True, schema["enum"][0]
    return False, None


def _parameters(spec: OpenAPISpec, op: Operation) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*(op.path_item.get("parameters") or []), *(op.raw.get("parameters") or [])]:
        p = spec.resolve(raw)
        if isinstance(p, dict) and "name" in p and "in" in p:
            merged[(p["in"], p["name"])] = p
    return list(merged.values())


def _auth_for(spec: OpenAPISpec, prefix: str, result: ImportResult) -> dict[str, Any] | None:
    requirements = spec.raw.get("security") or []
    schemes = (spec.raw.get("components") or {}).get("securitySchemes") or {}
    if not requirements:
        for op in spec.operations():
            if op.raw.get("security"):
                requirements = op.raw["security"]
                break
    for requirement in requirements:
        for scheme_name in requirement or {}:
            scheme = spec.resolve(schemes.get(scheme_name) or {})
            kind = scheme.get("type")
            if kind == "http" and str(scheme.get("scheme", "")).lower() == "bearer":
                result.secrets[f"{prefix}_TOKEN"] = f"Bearer token for {spec.title}"
                return {"type": "bearer", "token": f"{{{{ secrets.{prefix}_TOKEN }}}}"}
            if kind == "http" and str(scheme.get("scheme", "")).lower() == "basic":
                result.secrets[f"{prefix}_USERNAME"] = f"Username for {spec.title}"
                result.secrets[f"{prefix}_PASSWORD"] = f"Password for {spec.title}"
                return {
                    "type": "basic",
                    "username": f"{{{{ secrets.{prefix}_USERNAME }}}}",
                    "password": f"{{{{ secrets.{prefix}_PASSWORD }}}}",
                }
            if kind == "apiKey" and scheme.get("in") in ("header", "query"):
                result.secrets[f"{prefix}_API_KEY"] = f"API key for {spec.title}"
                return {
                    "type": "api_key",
                    "name": scheme.get("name", "X-API-Key"),
                    "in": scheme["in"],
                    "value": f"{{{{ secrets.{prefix}_API_KEY }}}}",
                }
            if kind == "oauth2" and "clientCredentials" in (scheme.get("flows") or {}):
                flow = scheme["flows"]["clientCredentials"]
                result.secrets[f"{prefix}_CLIENT_ID"] = f"OAuth2 client id for {spec.title}"
                result.secrets[f"{prefix}_CLIENT_SECRET"] = f"OAuth2 client secret for {spec.title}"
                auth: dict[str, Any] = {
                    "type": "oauth2_client_credentials",
                    "token_url": flow.get("tokenUrl", PLACEHOLDER),
                    "client_id": f"{{{{ secrets.{prefix}_CLIENT_ID }}}}",
                    "client_secret": f"{{{{ secrets.{prefix}_CLIENT_SECRET }}}}",
                }
                return auth
            result.notes.append(
                f"security scheme '{scheme_name}' ({kind}) is not supported yet: set auth by hand"
            )
    return None


def generate(spec: OpenAPISpec, service: str | None = None, base_url: str | None = None) -> ImportResult:
    service_name = slug(service or spec.title)
    result = ImportResult(service=service_name, service_block={}, monitors=[])

    server = base_url or next((s for s in spec.servers if s.startswith(("http://", "https://"))), None)
    if server and "{" not in server:
        result.service_block["base_url"] = server.rstrip("/")
    else:
        result.notes.append(
            "no absolute server URL in the spec: set base_url on the service or an environment"
        )

    prefix = re.sub(r"[^A-Z0-9]+", "_", service_name.upper()).strip("_")
    auth = _auth_for(spec, prefix, result)
    if auth:
        result.service_block["auth"] = auth

    used: set[str] = set()
    for op in spec.operations():
        if op.method not in SAFE_METHODS:
            result.skipped_unsafe += 1
            continue
        if op.raw.get("deprecated"):
            result.notes.append(f"skipped deprecated operation {op.method.upper()} {op.path}")
            continue
        base = (
            slug(op.operation_id)
            if op.operation_id
            else slug(f"{op.method} {op.path.replace('{', '').replace('}', '')}")
        )
        name = base
        n = 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)

        url = op.path
        params: dict[str, Any] = {}
        variables: dict[str, Any] = {}
        enabled = True
        for p in _parameters(spec, op):
            if p["in"] not in ("path", "query") or (p["in"] == "query" and not p.get("required")):
                continue
            found, value = _example(spec, p)
            if not found:
                enabled = False
                value = PLACEHOLDER
            var = re.sub(r"[^A-Za-z0-9_]", "_", p["name"])
            variables[var] = value
            ref = f"{{{{ vars.{var} }}}}"
            if p["in"] == "path":
                url = url.replace("{" + p["name"] + "}", ref)
            else:
                params[p["name"]] = ref

        responses = op.raw.get("responses") or {}
        ok_codes = sorted(int(c) for c in responses if str(c).isdigit() and str(c).startswith("2"))
        status: int | str = ok_codes[0] if ok_codes else "2xx"

        request: dict[str, Any] = {"method": op.method.upper(), "url": url}
        if params:
            request["params"] = params
        monitor: dict[str, Any] = {"name": name}
        if op.raw.get("summary"):
            monitor["description"] = str(op.raw["summary"])
        if not enabled:
            monitor["enabled"] = False
        if op.raw.get("tags"):
            monitor["tags"] = [slug(t) for t in op.raw["tags"]]
        if variables:
            monitor["vars"] = variables
        monitor["request"] = request
        monitor["assert"] = [{"status": status}]
        monitor["drift"] = {"enabled": True}
        result.monitors.append(monitor)
        if not enabled:
            result.notes.append(
                f"{name}: set the {PLACEHOLDER} values in 'vars', then remove 'enabled: false'"
            )
    return result
