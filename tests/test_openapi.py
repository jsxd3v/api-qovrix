import asyncio

import pytest
import respx

from api_qovrix.config.loader import load_project
from api_qovrix.engine.runner import RunOptions, run_monitors, select_monitors
from api_qovrix.errors import ConfigError
from api_qovrix.importers.openapi import generate
from api_qovrix.openapi.spec import OpenAPISpec

SPEC_30 = {
    "openapi": "3.0.3",
    "info": {"title": "Shop API", "version": "1"},
    "servers": [{"url": "https://api.shop.test/v1"}],
    "security": [{"bearer": []}],
    "components": {
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "Order": {
                "type": "object",
                "required": ["id", "total"],
                "properties": {
                    "id": {"type": "integer"},
                    "total": {"type": "number"},
                    "coupon": {"type": "string", "nullable": True},
                    "nullable": {"type": "boolean"},
                },
            }
        },
        "responses": {
            "OrderList": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}
                    }
                },
            }
        },
        "parameters": {
            "OrderId": {
                "name": "orderId",
                "in": "path",
                "required": True,
                "schema": {"type": "integer", "example": 7},
            }
        },
    },
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "tags": ["Orders"],
                "responses": {"200": {"$ref": "#/components/responses/OrderList"}},
            },
            "post": {"responses": {"201": {"description": "created"}}},
        },
        "/orders/{orderId}": {
            "parameters": [{"$ref": "#/components/parameters/OrderId"}],
            "get": {
                "operationId": "getOrder",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}},
                    },
                    "404": {"description": "missing"},
                },
            },
        },
        "/search": {
            "get": {
                "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/old": {"get": {"deprecated": True, "responses": {"200": {"description": "ok"}}}},
    },
}


def test_validate_30_with_refs_and_nullable():
    spec = OpenAPISpec(SPEC_30)
    url = "https://api.shop.test/v1/orders/7"
    assert spec.validate("GET", url, 200, {"id": 7, "total": 1.5, "coupon": None}, True) == (True, "")
    ok, msg = spec.validate("GET", url, 200, {"id": "7", "total": 1.5}, True)
    assert not ok and "$.id" in msg and "GET /orders/{orderId} 200" in msg
    ok, msg = spec.validate("GET", "https://api.shop.test/v1/orders", 200, [{"id": 1}], True)
    assert not ok and "total" in msg
    assert spec.validate("GET", url, 404, None, False) == (True, "")
    ok, msg = spec.validate("GET", url, 500, None, False)
    assert not ok and "not documented" in msg
    assert spec.validate("GET", "https://api.shop.test/v1/unknown", 200, {}, True) == (
        True,
        "not in the OpenAPI spec",
    )


def test_validate_31():
    spec = OpenAPISpec(
        {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/a": {
                    "get": {
                        "responses": {
                            "2XX": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"n": {"type": ["integer", "null"]}},
                                            "required": ["n"],
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            },
        }
    )
    assert spec.validate("GET", "https://x/a", 200, {"n": None}, True)[0]
    assert not spec.validate("GET", "https://x/a", 201, {"n": "1"}, True)[0]


def test_swagger_2_rejected(tmp_path):
    (tmp_path / "s.json").write_text('{"swagger": "2.0"}')
    with pytest.raises(ConfigError, match=r"Swagger 2\.0"):
        OpenAPISpec.load(str(tmp_path / "s.json"))


def test_import_generates_safe_monitors():
    result = generate(OpenAPISpec(SPEC_30))
    assert result.service == "shop-api"
    assert result.service_block["base_url"] == "https://api.shop.test/v1"
    assert result.service_block["auth"] == {"type": "bearer", "token": "{{ secrets.SHOP_API_TOKEN }}"}
    names = {m["name"]: m for m in result.monitors}
    assert set(names) == {"list-orders", "get-order", "get-search"}
    assert names["get-order"]["request"]["url"] == "/orders/{{ vars.orderId }}"
    assert names["get-order"]["vars"] == {"orderId": 7}
    assert names["get-search"]["enabled"] is False
    assert names["get-search"]["request"]["params"] == {"q": "{{ vars.q }}"}
    assert result.skipped_unsafe == 1
    assert "SHOP_API_TOKEN" in result.secrets


@respx.mock
def test_runner_validates_against_spec(make_project):
    import json

    root = make_project(
        """
        version: 1
        openapi: { source: openapi.json }
        """,
        {
            "openapi.json": json.dumps(SPEC_30),
            "monitors/shop.yaml": """
                service: { base_url: https://api.shop.test/v1 }
                monitors:
                  - name: order
                    request: { url: /orders/7 }
                  - name: no-validate
                    openapi: false
                    request: { url: /orders/8 }
            """,
        },
    )
    respx.get("https://api.shop.test/v1/orders/7").respond(200, json={"id": "7"})
    respx.get("https://api.shop.test/v1/orders/8").respond(200, json={"id": "8"})
    project = load_project(root / "api-qovrix.yaml")
    summary = asyncio.run(run_monitors(project, select_monitors(project), RunOptions()))
    by = {r.name: r for r in summary.results}
    assert by["order"].status == "fail" and "openapi schema" in by["order"].failure_summary
    assert by["no-validate"].status == "pass"
