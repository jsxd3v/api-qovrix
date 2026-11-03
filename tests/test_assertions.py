from pathlib import Path

import httpx
import pytest

from api_qovrix.config.models import Assertion
from api_qovrix.engine.assertions import ResponseView, evaluate

BODY = {"status": "paid", "total": 12.5, "items": [{"id": 1}, {"id": 2}], "tags": ["a", "b"], "note": None}


def view(status=200, body=BODY, headers=None, elapsed=120.0):
    import json

    return ResponseView(
        status_code=status,
        headers=httpx.Headers(headers or {"Content-Type": "application/json"}),
        elapsed_ms=elapsed,
        text=json.dumps(body),
        json=body,
        is_json=True,
    )


def check(spec, v=None):
    return evaluate(Assertion.model_validate(spec), v or view(), Path("."))


@pytest.mark.parametrize(
    "spec,ok",
    [
        ({"status": 200}, True),
        ({"status": "2xx"}, True),
        ({"status": [201, 204]}, False),
        ({"response_time_ms": 500}, True),
        ({"response_time_ms": {"lt": 100}}, False),
        ({"jsonpath": "$.status", "equals": "paid"}, True),
        ({"jsonpath": "$.status", "in": ["pending", "paid"]}, True),
        ({"jsonpath": "$.status", "not_in": ["paid"]}, False),
        ({"jsonpath": "$.total", "gt": 10, "lt": 20}, True),
        ({"jsonpath": "$.total", "gte": 13}, False),
        ({"jsonpath": "$.items", "length": 2}, True),
        ({"jsonpath": "$.items[*].id", "contains": 2}, True),
        ({"jsonpath": "$.tags", "not_contains": "c"}, True),
        ({"jsonpath": "$.missing", "exists": False}, True),
        ({"jsonpath": "$.missing", "equals": 1}, False),
        ({"jsonpath": "$.note", "exists": True}, True),
        ({"jsonpath": "$.note", "type": "null"}, True),
        ({"jsonpath": "$.status", "matches": "^pa"}, True),
        ({"jsonpath": "$.items", "not_empty": True}, True),
        ({"jsonpath": "$.total", "type": "number"}, True),
        ({"header": "content-type", "contains": "json"}, True),
        ({"header": "x-missing", "exists": True}, False),
        ({"body": {"contains": "paid"}}, True),
        ({"body": {"not_contains": "paid"}}, False),
        ({"json_schema": {"type": "object", "required": ["status"]}}, True),
        ({"json_schema": {"type": "object", "properties": {"total": {"type": "string"}}}}, False),
    ],
)
def test_operators(spec, ok):
    result = check(spec)
    assert result.passed is ok, result


def test_failure_messages_are_readable():
    r = check({"jsonpath": "$.status", "equals": "pending"})
    assert r.message == "expected 'pending', got 'paid'"
    r = check({"status": 201})
    assert r.message == "got 200"


def test_non_json_response():
    v = ResponseView(200, httpx.Headers({}), 1.0, "<html>", None, False)
    assert not check({"jsonpath": "$.a", "equals": 1}, v).passed
    assert check({"jsonpath": "$.a", "exists": False}, v).passed


def test_schema_file(tmp_path):
    (tmp_path / "s.json").write_text('{"type": "object", "required": ["status"]}')
    a = Assertion.model_validate({"json_schema": "s.json"})
    assert evaluate(a, view(), tmp_path).passed
