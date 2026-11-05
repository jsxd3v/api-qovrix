"""Load `api-qovrix.yaml` and monitor files into a resolved Project.

Setting precedence (base_url, auth, headers, timeout, retries, interval, notify, vars):
    step request -> monitor -> service -> environment -> defaults
A full URL in `request.url` ignores base_url.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from api_qovrix.config.models import (
    Auth,
    DriftConfig,
    Environment,
    Monitor,
    MonitorFile,
    ProjectConfig,
    ServiceConfig,
    Step,
)
from api_qovrix.config.templating import Secrets
from api_qovrix.errors import ConfigError

CONFIG_NAMES = ("api-qovrix.yaml", "api-qovrix.yml")
ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class ResolvedMonitor:
    monitor: Monitor
    service: str
    source: Path
    steps: list[Step]
    base_url: str | None
    auth: Auth | None
    headers: dict[str, str]
    timeout: float
    retries: int
    interval: float
    notify: list[str]
    vars: dict[str, Any]
    tags: list[str]
    drift: DriftConfig | None
    validate_openapi: bool
    alert_after: int = 2
    alert_repeat: float | None = None

    @property
    def name(self) -> str:
        return self.monitor.name


@dataclass
class Project:
    root: Path
    config_path: Path
    config: ProjectConfig
    environment: str | None
    secrets: Secrets
    services: dict[str, ServiceConfig]
    service_sources: dict[str, Path]
    monitors: list[ResolvedMonitor] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def baselines_dir(self) -> Path:
        return self.root / ".api-qovrix" / "baselines"

    def monitor(self, name: str) -> ResolvedMonitor:
        for m in self.monitors:
            if m.name == name:
                return m
        known = ", ".join(sorted(m.name for m in self.monitors)) or "none"
        raise ConfigError(f"no monitor named '{name}' (known: {known})")


def find_config(path: str | Path | None = None, start: Path | None = None) -> Path:
    if path:
        p = Path(path)
        if p.is_dir():
            for name in CONFIG_NAMES:
                if (p / name).is_file():
                    return p / name
            raise ConfigError(f"no api-qovrix.yaml in {p}")
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        for name in CONFIG_NAMES:
            if (folder / name).is_file():
                return folder / name
    raise ConfigError(
        "no api-qovrix.yaml found here or in a parent folder. Run 'api-qovrix init' to create one."
    )


def substitute_env(value: Any, sources: dict[str, str] | None = None, key: str | None = None) -> Any:
    """Replace ${env:NAME} and ${env:NAME:-default}. Unset variables without default become None."""
    if isinstance(value, str):
        whole = ENV_RE.fullmatch(value.strip())
        if whole:
            if sources is not None and key is not None:
                sources[key] = whole.group(1)
            return os.environ.get(whole.group(1), whole.group(2))

        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")

        return ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_env(v) for v in value]
    return value


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing variables."""
    if not path.is_file():
        raise ConfigError(f"env file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from None


def format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if not str(p).startswith("function-"))
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  - {loc or '(root)'}: {msg}")
    return "\n".join(lines)


def _first[T](*values: T | None) -> T | None:
    for v in values:
        if v is not None:
            return v
    return None


def load_project(
    config: str | Path | None = None,
    environment: str | None = None,
    env_file: str | Path | None = None,
) -> Project:
    config_path = find_config(config)
    root = config_path.parent.resolve()
    if env_file:
        load_dotenv(Path(env_file))

    raw = _read_yaml(config_path) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: expected a mapping at the top level")

    secret_sources: dict[str, str] = {}
    raw_secrets = raw.get("secrets") or {}
    if not isinstance(raw_secrets, dict):
        raise ConfigError(f"{config_path}: 'secrets' must be a mapping of NAME: ${{env:VARIABLE}}")
    secret_values: dict[str, str | None] = {}
    for name, value in raw_secrets.items():
        if not isinstance(value, str):
            raise ConfigError(f"{config_path}: secret '{name}' must be a string like ${{env:VARIABLE}}")
        if not ENV_RE.search(value):
            raise ConfigError(
                f"{config_path}: secret '{name}' must come from an environment variable, "
                f"e.g. {name}: ${{env:{name}}}. Never put secret values in this file."
            )
        secret_values[name] = substitute_env(value, secret_sources, name)

    raw_wo_secrets = {k: v for k, v in raw.items() if k != "secrets"}
    try:
        cfg = ProjectConfig.model_validate({**substitute_env(raw_wo_secrets), "secrets": raw_secrets})
    except ValidationError as exc:
        raise ConfigError(format_validation_error(config_path, exc)) from None

    env_name = environment or os.environ.get("API_QOVRIX_ENV") or cfg.default_environment
    if env_name is None and cfg.environments:
        env_name = next(iter(cfg.environments))
    env: Environment | None = None
    if env_name is not None:
        if env_name not in cfg.environments:
            known = ", ".join(cfg.environments) or "none defined"
            raise ConfigError(f"unknown environment '{env_name}' (known: {known})")
        env = cfg.environments[env_name]

    project = Project(
        root=root,
        config_path=config_path,
        config=cfg,
        environment=env_name,
        secrets=Secrets(secret_values, secret_sources),
        services=dict(cfg.services),
        service_sources={name: config_path for name in cfg.services},
    )

    files: list[Path] = []
    for pattern in cfg.include:
        for p in sorted(root.glob(pattern)):
            is_monitor_file = p.is_file() and p.suffix in (".yaml", ".yml")
            if is_monitor_file and p.resolve() != config_path.resolve() and p not in files:
                files.append(p)

    parsed: list[tuple[Path, MonitorFile]] = []
    for path in files:
        data = _read_yaml(path)
        if data is None:
            continue
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a mapping with 'monitors:'")
        try:
            mf = MonitorFile.model_validate(substitute_env(data))
        except ValidationError as exc:
            raise ConfigError(format_validation_error(path, exc)) from None
        if mf.service is not None:
            name = path.stem
            if name in project.services:
                where = project.service_sources[name]
                raise ConfigError(
                    f"service '{name}' is defined twice: in {where.relative_to(root)} and in "
                    f"{path.relative_to(root)}. Define it in one place only."
                )
            project.services[name] = mf.service
            project.service_sources[name] = path
        parsed.append((path, mf))

    from api_qovrix.server.cron import Cron, CronError, timezone

    try:
        timezone(cfg.server.timezone)
    except CronError as exc:
        raise ConfigError(f"{config_path}: server.timezone: {exc}") from None

    seen: dict[str, Path] = {}
    known_notifiers = set(cfg.notifiers)
    for path, mf in parsed:
        for mon in mf.monitors:
            if mon.name in seen:
                raise ConfigError(
                    f"monitor name '{mon.name}' is used twice ({seen[mon.name].relative_to(root)} and "
                    f"{path.relative_to(root)}). Monitor names must be unique."
                )
            seen[mon.name] = path
            if mon.cron:
                try:
                    Cron.parse(mon.cron)
                except CronError as exc:
                    raise ConfigError(f"{path.relative_to(root)}: monitor '{mon.name}': {exc}") from None
            resolved = _resolve(mon, path, project, env)
            unknown = [n for n in resolved.notify if n not in known_notifiers]
            if unknown:
                raise ConfigError(
                    f"{path.relative_to(root)}: monitor '{mon.name}' uses unknown notifier(s): "
                    f"{', '.join(unknown)}"
                )
            project.monitors.append(resolved)

    source = cfg.openapi.source if cfg.openapi else None
    if source and not source.startswith(("http://", "https://")) and not (root / source).is_file():
        raise ConfigError(f"openapi.source not found: {source}")
    return project


def _resolve(mon: Monitor, path: Path, project: Project, env: Environment | None) -> ResolvedMonitor:
    cfg = project.config
    service_name = mon.service or path.stem
    svc = project.services.get(service_name, ServiceConfig())
    d = cfg.defaults

    headers = {**d.headers, **(env.headers if env else {}), **svc.headers, **mon.headers}
    variables = {**(env.vars if env else {}), **svc.vars, **mon.vars}
    drift = mon.drift or d.drift
    if drift is not None and not drift.enabled:
        drift = None

    return ResolvedMonitor(
        monitor=mon,
        service=service_name,
        source=path,
        steps=mon.normalized_steps(),
        base_url=_first(mon.base_url, svc.base_url, env.base_url if env else None),
        auth=mon.auth if mon.auth is not None else svc.auth,
        headers=headers,
        timeout=_first(mon.timeout, svc.timeout, d.timeout) or 10.0,
        retries=_first(mon.retries, svc.retries, d.retries) or 0,
        interval=_first(mon.interval, svc.interval, d.interval) or 60.0,
        notify=_first(mon.notify, svc.notify, d.notify) or [],
        vars=variables,
        tags=sorted({*svc.tags, *mon.tags}),
        drift=drift,
        validate_openapi=_first(mon.openapi, cfg.openapi.validate_responses if cfg.openapi else None)
        or False,
        alert_after=_first(mon.alert_after, svc.alert_after, d.alert_after) or 2,
        alert_repeat=_first(mon.alert_repeat, svc.alert_repeat, d.alert_repeat),
    )
