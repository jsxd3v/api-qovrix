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


class TokenRequestSpec(Model):
    """The login call that returns a token."""

    method: str = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = Field(None, alias="json")
    form: dict[str, Any] | None = None
    timeout: Duration | None = None


class TokenRequestAuth(Model):
    """Log in with any request that returns a token, once, and reuse the token until it expires.

    Use it on a service so all its monitors share one login.
    """

    type: Literal["token_request"]
    request: TokenRequestSpec
    token: str = Field("$.access_token", description="JSONPath to the token in the login response.")
    expires_in: str | None = Field(
        "$.expires_in", description="JSONPath to the token lifetime in seconds (missing: use 'lifetime')."
    )
    lifetime: Duration = Field(
        3600.0, description="Token lifetime when the response doesn't say, e.g. '50m'."
    )
    header: str = Field("Authorization", description="Header that carries the token.")
    prefix: str = Field("Bearer ", description="Text before the token in that header.")


Auth = Annotated[
    BearerAuth | BasicAuth | ApiKeyAuth | OAuth2ClientCredentials | TokenRequestAuth,
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
    description: str | None = None
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
    cron: str | None = Field(
        None,
        description="Schedule for `serve` instead of 'interval', e.g. '0 8 * * mon-fri' (server.timezone).",
    )
    timeout: Duration | None = None
    retries: int | None = Field(None, ge=0, le=10)
    notify: list[str] | None = None
    alert_after: int | None = Field(
        None, ge=1, le=100, description="Failures in a row before alerting (serve)."
    )
    alert_repeat: Duration | None = Field(None, description="Remind while still down, e.g. '1h' (serve).")
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
    alert_after: int | None = Field(
        None, ge=1, le=100, description="Failures in a row before alerting (serve)."
    )
    alert_repeat: Duration | None = Field(None, description="Remind while still down, e.g. '1h' (serve).")
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
    alert_after: int = Field(
        2, ge=1, le=100, description="Failures in a row before a monitor is 'down' (serve)."
    )
    alert_repeat: Duration | None = Field(None, description="Remind while still down, e.g. '1h' (serve).")
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
    format: Literal["json", "embed", "blocks"] = Field(
        "json",
        description="json: full details with a 'text' field. embed: a colored card ('embeds'). "
        "blocks: header, fields and a button ('blocks'). Ignored when 'body' is set.",
    )
    body: Any = Field(
        None,
        description="Optional custom JSON body. Strings may use {{ alert.text }}, "
        "{{ alert.failed_count }}, ... Default: a JSON object with a 'text' field and the full details.",
    )
    timeout: Duration = 10.0


NotifierConfig = Annotated[EmailNotifier | WebhookNotifier, Field(discriminator="type")]


class HeartbeatSettings(Model):
    """Ping a URL while `serve` is healthy, so an outside service can alert you when the pings stop."""

    url: str | None = Field(None, description="Use ${env:...}: these URLs usually contain a secret token.")
    interval: Duration = Field(300.0, description="How often to ping, e.g. '5m'.")
    method: Literal["GET", "POST", "HEAD"] = "GET"
    timeout: Duration = 10.0


HEX_COLOR = r"^#[0-9a-fA-F]{6}$"


class Branding(Model):
    """Name, logo and color for the dashboard, the status page and alert emails."""

    name: str | None = Field(None, max_length=80, description="Shown instead of the project name.")
    logo: str | None = Field(
        None, description="Path to a .svg, .png, .jpg or .webp file (relative to api-qovrix.yaml)."
    )
    accent: str = Field("#5b4fd6", pattern=HEX_COLOR, description="Accent color, e.g. '#0e7490'.")


class StatusService(Model):
    service: str = Field(description="Service name in your monitor files.")
    name: str | None = Field(None, max_length=80, description="Public name (default: the service name).")
    description: str | None = Field(None, max_length=200)


class StatusNotice(Model):
    """A message from the operator on the status page."""

    title: str = Field(max_length=160)
    message: str | None = Field(None, max_length=2000)
    status: Literal["investigating", "identified", "monitoring", "resolved", "maintenance"] = "investigating"
    started: str = Field(description="When it started, ISO 8601, e.g. '2026-11-05T10:00:00Z'.")
    resolved: str | None = Field(None, description="When it ended (ISO 8601). Empty = still ongoing.")
    services: list[str] = Field(default_factory=list, description="Affected services (empty = all).")

    @model_validator(mode="after")
    def _check(self) -> StatusNotice:
        from datetime import datetime

        for label, value in (("started", self.started), ("resolved", self.resolved)):
            if value is None:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"{label}: '{value}' is not an ISO 8601 date/time") from None
            if parsed.tzinfo is None:
                raise ValueError(f"{label}: add a time zone, e.g. '{value}Z' for UTC")
        return self


class StatusPage(Model):
    """A public, read-only status page at /status (no login, no internal details)."""

    enabled: bool = False
    title: str | None = Field(None, max_length=120, description="Default: '<name> status'.")
    description: str | None = Field(None, max_length=500)
    services: list[StatusService] = Field(
        default_factory=list, description="Services to show, in this order. Others stay private."
    )
    days: int = Field(90, ge=7, le=365, description="Days of uptime history to show.")
    show_incidents: bool = Field(True, description="List detected outages (times only, no error details).")
    notices: list[StatusNotice] = Field(default_factory=list)


class ServerSettings(Model):
    """Settings for `api-qovrix serve`."""

    retention: Duration = Field(30 * 86400.0, description="How long run history is kept, e.g. '30d'.")
    group_wait: Duration = Field(
        10.0, description="Wait this long to group changes of the same service into one alert."
    )
    concurrency: int = Field(10, ge=1, le=500, description="Monitors running at the same time.")
    timezone: str = Field("UTC", description="Time zone for cron schedules, e.g. 'Asia/Manila'.")
    public_url: str | None = Field(
        None,
        description="Public address of the dashboard (e.g. https://monitor.example.com) for links in alerts.",
    )
    heartbeat: HeartbeatSettings | None = None
    watch: bool = Field(True, description="Reload monitor files automatically when they change.")


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
    server: ServerSettings = Field(default_factory=ServerSettings)
    branding: Branding = Field(default_factory=Branding)
    status_page: StatusPage = Field(default_factory=StatusPage)
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
