"""Pydantic models for `api-qovrix.yaml` and monitor files.

These models are also the source of the published JSON Schema (`api-qovrix schema`).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema, model_validator

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(value: Any) -> Any:
    """'500ms', '10s', '1m', '2h' or a number of seconds -> seconds as float."""
    if isinstance(value, bool):
        raise ValueError("expected a duration like '10s' or '1m'")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m:
            return float(m.group(1)) * _UNITS[m.group(2)]
    raise ValueError(f"invalid duration {value!r}: use e.g. '500ms', '10s', '1m', '2h'")


Duration = Annotated[
    float,
    BeforeValidator(parse_duration),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string", "pattern": r"^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)?\s*$"},
                {"type": "number", "minimum": 0},
            ],
            "description": "Duration, e.g. '500ms', '10s', '1m', '2h' (a number means seconds).",
        }
    ),
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- auth


class BearerAuth(Model):
    type: Literal["bearer"]
    token: str


class BasicAuth(Model):
    type: Literal["basic"]
    username: str
    password: str


class ApiKeyAuth(Model):
    type: Literal["api_key"]
    name: str = Field(description="Header or query parameter name, e.g. 'X-API-Key'.")
    value: str
    location: Literal["header", "query"] = Field("header", alias="in")


class OAuth2ClientCredentials(Model):
    type: Literal["oauth2_client_credentials"]
    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    audience: str | None = None
    client_auth: Literal["basic", "post"] = Field(
        "basic", description="Send client credentials as HTTP basic auth (default) or in the form body."
    )
    extra_params: dict[str, str] = Field(default_factory=dict)


Auth = Annotated[
    BearerAuth | BasicAuth | ApiKeyAuth | OAuth2ClientCredentials,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- request


class Request(Model):
    method: str = "GET"
    url: str = Field(description="Full URL, or a path joined to the service/environment base_url.")
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict, description="Query string parameters.")
    json_body: Any = Field(None, alias="json")
    body: str | None = None
    form: dict[str, Any] | None = None
    auth: Auth | None = None
    timeout: Duration | None = None
    follow_redirects: bool = True
    verify_tls: bool = True

    @model_validator(mode="after")
    def _check(self) -> Request:
        self.method = self.method.upper()
        given = [n for n in ("json_body", "body", "form") if getattr(self, n) is not None]
        if len(given) > 1:
            raise ValueError("use only one of 'json', 'body' or 'form'")
        return self


# --------------------------------------------------------------------------- assertions

OPERATORS = (
    "equals",
    "not_equals",
    "in_",
    "not_in",
    "contains",
    "not_contains",
    "matches",
    "exists",
    "gt",
    "gte",
    "lt",
    "lte",
    "length",
    "type",
    "not_empty",
)
TARGETS = ("status", "response_time_ms", "jsonpath", "header", "body", "json_schema")


class Comparison(Model):
    lt: float | None = None
    lte: float | None = None
    gt: float | None = None
    gte: float | None = None
    eq: float | None = None


class BodyCheck(Model):
    contains: str | None = None
    not_contains: str | None = None
    matches: str | None = None


StatusSpec = int | str | list[int | str]


class Assertion(Model):
    """One check. Exactly one target: status, response_time_ms, jsonpath, header, body or json_schema."""

    status: StatusSpec | None = Field(None, description="200, '2xx' or a list like [200, 201].")
    response_time_ms: Comparison | float | None = Field(
        None, description="Upper limit in ms, or {lt|lte|gt|gte|eq: ms}."
    )
    jsonpath: str | None = Field(None, description="JSONPath into the response body, e.g. $.data[0].id")
    header: str | None = Field(None, description="Response header name (case-insensitive).")
    body: BodyCheck | None = None
    json_schema: dict[str, Any] | str | None = Field(
        None, description="Inline JSON Schema, or a path to a schema file relative to the project root."
    )

    equals: Any = None
    not_equals: Any = None
    in_: list[Any] | None = Field(None, alias="in")
    not_in: list[Any] | None = None
    contains: Any = None
    not_contains: Any = None
    matches: str | None = None
    exists: bool | None = None
    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None
    length: int | None = None
    type: Literal["string", "integer", "number", "boolean", "array", "object", "null"] | None = None
    not_empty: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Assertion:
        targets = [t for t in TARGETS if t in self.model_fields_set]
        if len(targets) != 1:
            raise ValueError(f"an assertion needs exactly one of: {', '.join(TARGETS)}")
        ops = self.operators()
        if targets[0] in ("jsonpath", "header"):
            if not ops:
                raise ValueError(
                    f"'{targets[0]}' needs an operator: {', '.join(o.rstrip('_') for o in OPERATORS)}"
                )
        elif ops:
            raise ValueError(
                f"operators like '{next(iter(ops)).rstrip('_')}' only apply to jsonpath or header"
            )
        return self

    @property
    def target(self) -> str:
        return next(t for t in TARGETS if t in self.model_fields_set)

    def operators(self) -> dict[str, Any]:
        return {o: getattr(self, o) for o in OPERATORS if o in self.model_fields_set}


# --------------------------------------------------------------------------- drift


class DriftConfig(Model):
    enabled: bool = True
    fail_on: Literal["breaking", "warning", "never"] = "breaking"
    min_samples: int = Field(5, ge=1, description="Samples needed before the learned shape is trusted.")
    ignore: list[str] = Field(
        default_factory=list, description="JSONPaths to skip, e.g. '$.meta.request_id' or '$..updated_at'."
    )


# --------------------------------------------------------------------------- monitors


class Step(Model):
    name: str
    request: Request
    assertions: list[Assertion] = Field(default_factory=list, alias="assert")
    extract: dict[str, str] = Field(
        default_factory=dict,
        description="Save values for later steps: JSONPath ('$.id') or 'header:<name>'.",
    )
    always_run: bool = Field(False, description="Run even when an earlier step failed (cleanup).")
    drift: DriftConfig | None = None


class Monitor(Model):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    service: str | None = Field(None, description="Defaults to the monitor file's name.")
    description: str | None = None
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    base_url: str | None = None
    auth: Auth | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    interval: Duration | None = None
    cron: str | None = None
    timeout: Duration | None = None
    retries: int | None = Field(None, ge=0, le=10)
    notify: list[str] | None = None
    vars: dict[str, Any] = Field(default_factory=dict)
    openapi: bool | None = Field(None, description="Validate responses against the project's OpenAPI spec.")

    request: Request | None = None
    assertions: list[Assertion] = Field(default_factory=list, alias="assert")
    extract: dict[str, str] = Field(default_factory=dict)
    drift: DriftConfig | None = None
    steps: list[Step] | None = None

    @model_validator(mode="after")
    def _check(self) -> Monitor:
        if (self.request is None) == (self.steps is None):
            raise ValueError("a monitor needs either 'request' (single check) or 'steps' (flow), not both")
        if self.steps is not None:
            if not self.steps:
                raise ValueError("'steps' must not be empty")
            if self.assertions or self.extract:
                raise ValueError("in a flow, put 'assert' and 'extract' on each step")
            names = [s.name for s in self.steps]
            if len(names) != len(set(names)):
                raise ValueError("step names must be unique within a monitor")
        return self

    def normalized_steps(self) -> list[Step]:
        if self.steps is not None:
            return self.steps
        assert self.request is not None
        return [Step(name="main", request=self.request, assertions=self.assertions, extract=self.extract)]


# --------------------------------------------------------------------------- project


class ServiceConfig(Model):
    base_url: str | None = None
    auth: Auth | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: Duration | None = None
    retries: int | None = Field(None, ge=0, le=10)
    interval: Duration | None = None
    notify: list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    vars: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, str] = Field(default_factory=dict, description="e.g. {status: ..., docs: ...}")


class Environment(Model):
    base_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    vars: dict[str, Any] = Field(default_factory=dict)


class Defaults(Model):
    interval: Duration = 60.0
    timeout: Duration = 10.0
    retries: int = Field(0, ge=0, le=10)
    notify: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    drift: DriftConfig | None = Field(None, description="Default drift settings for every monitor.")


class OpenAPIConfig(Model):
    source: str = Field(description="Path (relative to the project) or URL of the OpenAPI document.")
    validate_responses: bool = True


class EmailNotifier(Model):
    """Send alerts by email through any SMTP server."""

    type: Literal["email"]
    to: list[str] = Field(min_length=1)
    smtp_url: str | None = Field(
        None,
        description="smtp://user:password@host:587 (STARTTLS when offered) or "
        "smtps://user:password@host:465. Use ${env:SMTP_URL}: it contains a password.",
    )
    sender: str | None = Field(None, alias="from", description="From address (default: the SMTP user).")
    subject_prefix: str = "[api-qovrix]"


class WebhookNotifier(Model):
    """POST a JSON message to a URL. Most chat and incident tools accept incoming webhooks."""

    type: Literal["webhook"]
    url: str | None = Field(None, description="Use ${env:...}: webhook URLs usually contain a secret token.")
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(
        None,
        description="Optional custom JSON body. Strings may use {{ alert.text }}, "
        "{{ alert.failed_count }}, ... Default: a JSON object with a 'text' field and the full details.",
    )
    timeout: Duration = 10.0


NotifierConfig = Annotated[EmailNotifier | WebhookNotifier, Field(discriminator="type")]


class ProjectConfig(Model):
    version: Literal[1] = 1
    project: str | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    environments: dict[str, Environment] = Field(default_factory=dict)
    default_environment: str | None = None
    secrets: dict[str, str] = Field(
        default_factory=dict, description="NAME: ${env:VARIABLE}. Values never live in this file."
    )
    notifiers: dict[str, NotifierConfig] = Field(default_factory=dict)
    openapi: OpenAPIConfig | None = None
    services: dict[str, ServiceConfig] = Field(default_factory=dict)
    include: list[str] = Field(default_factory=lambda: ["monitors/**/*.yaml", "monitors/**/*.yml"])
    redact: list[str] = Field(
        default_factory=list, description="Extra JSON keys to mask in reports and baselines."
    )
    allowed_hosts: list[str] | None = Field(
        None,
        description="Optional allowlist of hosts monitors may call (e.g. 'api.example.com', '*.internal').",
    )

    @model_validator(mode="after")
    def _check(self) -> ProjectConfig:
        if self.default_environment and self.default_environment not in self.environments:
            raise ValueError(f"default_environment '{self.default_environment}' is not under 'environments'")
        return self


class MonitorFile(Model):
    """A file under monitors/. May define its own service (named after the file)."""

    service: ServiceConfig | None = None
    monitors: list[Monitor] = Field(default_factory=list)
