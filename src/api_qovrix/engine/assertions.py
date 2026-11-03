"""Evaluate assertions against a response."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from api_qovrix.config.models import Assertion, Comparison
from api_qovrix.engine.jsonpaths import MISSING, query
from api_qovrix.engine.results import AssertionResult
from api_qovrix.errors import ConfigError

SYMBOLS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=="}


@dataclass
class ResponseView:
    status_code: int
    headers: httpx.Headers
    elapsed_ms: float
    text: str
    json: Any
    is_json: bool


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = repr(text) if isinstance(value, str) else text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_matches(code: int, spec: int | str | list[int | str]) -> bool:
    specs = spec if isinstance(spec, list) else [spec]
    for s in specs:
        if isinstance(s, int) and code == s:
            return True
        if isinstance(s, str):
            s = s.strip().lower()
            if s.isdigit() and code == int(s):
                return True
            if len(s) == 3 and s.endswith("xx") and s[0].isdigit() and code // 100 == int(s[0]):
                return True
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _check_operator(op: str, expected: Any, actual: Any) -> str | None:
    """Return None when the operator passes, else a failure message."""
    if op == "exists":
        present = actual is not MISSING
        if present == bool(expected):
            return None
        return "is missing" if expected else f"exists ({short(actual)})"
    if actual is MISSING:
        return "no match in the response"

    if op == "equals":
        return None if actual == expected else f"expected {short(expected)}, got {short(actual)}"
    if op == "not_equals":
        return None if actual != expected else f"must not be {short(expected)}"
    if op == "in_":
        return None if actual in expected else f"{short(actual)} is not one of {short(expected)}"
    if op == "not_in":
        return None if actual not in expected else f"{short(actual)} must not be one of {short(expected)}"
    if op in ("contains", "not_contains"):
        if isinstance(actual, str):
            found = str(expected) in actual
        elif isinstance(actual, list | dict):
            found = expected in actual
        else:
            found = str(expected) in str(actual)
        if (op == "contains") == found:
            return None
        return (
            f"{short(actual)} does not contain {short(expected)}"
            if op == "contains"
            else (f"{short(actual)} contains {short(expected)}")
        )
    if op == "matches":
        try:
            ok = (
                re.search(str(expected), actual if isinstance(actual, str) else json.dumps(actual))
                is not None
            )
        except re.error as exc:
            raise ConfigError(f"invalid regex {expected!r}: {exc}") from None
        return None if ok else f"{short(actual)} does not match /{expected}/"
    if op in ("gt", "gte", "lt", "lte"):
        num = _number(actual)
        if num is None:
            return f"{short(actual)} is not a number"
        ok = {
            "gt": num > expected,
            "gte": num >= expected,
            "lt": num < expected,
            "lte": num <= expected,
        }[op]
        return None if ok else f"{short(actual)} is not {SYMBOLS[op]} {expected:g}"
    if op == "length":
        try:
            n = len(actual)
        except TypeError:
            return f"{short(actual)} has no length"
        return None if n == expected else f"length is {n}, expected {expected}"
    if op == "type":
        t = json_type(actual)
        ok = t == expected or (expected == "number" and t == "integer")
        return None if ok else f"type is {t}, expected {expected}"
    if op == "not_empty":
        empty = actual in (None, "", [], {})
        return (
            None
            if empty != bool(expected)
            else ("is empty" if expected else f"is not empty ({short(actual)})")
        )
    raise ConfigError(f"unknown operator {op}")  # pragma: no cover


def _describe(a: Assertion) -> str:
    target = a.target
    if target == "status":
        return f"status {a.status}"
    if target == "response_time_ms":
        return "response time"
    if target == "json_schema":
        return "json schema" if isinstance(a.json_schema, dict) else f"json schema {a.json_schema}"
    if target == "body":
        return "body"
    subject = a.jsonpath if target == "jsonpath" else f"header {a.header}"
    ops = ", ".join(f"{op.rstrip('_')} {short(v, 40)}" for op, v in a.operators().items())
    return f"{subject} {ops}"


def load_schema(spec: dict[str, Any] | str, root: Path) -> dict[str, Any]:
    if isinstance(spec, dict):
        return spec
    path = (root / spec).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"json_schema file not found: {spec}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"json_schema file {spec} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"json_schema file {spec} must contain a JSON object")
    return data


def schema_errors(validator: Any, instance: Any, limit: int = 3) -> list[str]:
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    out = []
    for err in errors[:limit]:
        where = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        out.append(f"{where}: {err.message}")
    if len(errors) > limit:
        out.append(f"(+{len(errors) - limit} more)")
    return out


def evaluate(a: Assertion, resp: ResponseView, root: Path) -> AssertionResult:
    name = _describe(a)
    target = a.target

    if target == "status":
        assert a.status is not None
        ok = _status_matches(resp.status_code, a.status)
        return AssertionResult(name, ok, "" if ok else f"got {resp.status_code}")

    if target == "response_time_ms":
        cmp = a.response_time_ms
        if not isinstance(cmp, Comparison):
            cmp = Comparison(lt=float(cmp)) if cmp is not None else Comparison()
        limits = {op: getattr(cmp, op) for op in SYMBOLS if getattr(cmp, op) is not None}
        checks = {
            "lt": lambda x, y: x < y,
            "lte": lambda x, y: x <= y,
            "gt": lambda x, y: x > y,
            "gte": lambda x, y: x >= y,
            "eq": lambda x, y: x == y,
        }
        failures = [
            f"{resp.elapsed_ms:.0f} ms is not {SYMBOLS[op]} {limit:g} ms"
            for op, limit in limits.items()
            if not checks[op](resp.elapsed_ms, limit)
        ]
        name = "response time " + " ".join(f"{SYMBOLS[op]} {limit:g} ms" for op, limit in limits.items())
        return AssertionResult(name, not failures, "; ".join(failures))

    if target == "body":
        assert a.body is not None
        failures = []
        if a.body.contains is not None and a.body.contains not in resp.text:
            failures.append(f"does not contain {short(a.body.contains)}")
        if a.body.not_contains is not None and a.body.not_contains in resp.text:
            failures.append(f"contains {short(a.body.not_contains)}")
        if a.body.matches is not None:
            try:
                if not re.search(a.body.matches, resp.text):
                    failures.append(f"does not match /{a.body.matches}/")
            except re.error as exc:
                raise ConfigError(f"invalid regex {a.body.matches!r}: {exc}") from None
        return AssertionResult(name, not failures, "; ".join(failures))

    if target == "json_schema":
        assert a.json_schema is not None
        if not resp.is_json:
            return AssertionResult(name, False, "response is not JSON")
        schema = load_schema(a.json_schema, root)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ConfigError(f"invalid json_schema: {exc.message}") from None
        errs = schema_errors(Draft202012Validator(schema), resp.json)
        return AssertionResult(name, not errs, "; ".join(errs))

    if target == "jsonpath":
        assert a.jsonpath is not None
        if not resp.is_json:
            actual: Any = MISSING
            if "exists" not in a.operators():
                return AssertionResult(name, False, "response is not JSON")
        else:
            actual = query(resp.json, a.jsonpath)
    else:
        assert a.header is not None
        actual = resp.headers.get(a.header, MISSING)

    failures = [
        msg
        for op, expected in a.operators().items()
        if (msg := _check_operator(op, expected, actual)) is not None
    ]
    return AssertionResult(name, not failures, "; ".join(failures))
