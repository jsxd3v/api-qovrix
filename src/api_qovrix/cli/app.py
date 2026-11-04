"""The `api-qovrix` command."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from api_qovrix import __version__
from api_qovrix.config.editing import add_secrets, dump_yaml, set_openapi_source
from api_qovrix.config.loader import Project, find_config, load_project
from api_qovrix.config.templating import references
from api_qovrix.drift.store import BaselineStore
from api_qovrix.engine.runner import BaselineMode, RunOptions, run_monitors, select_monitors
from api_qovrix.errors import QovrixError
from api_qovrix.report.output import print_summary, to_json, to_junit

app = typer.Typer(
    name="api-qovrix",
    help="Contract-aware API monitoring: catches breaking changes, not just downtime.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
import_app = typer.Typer(help="Generate monitors from other formats.", no_args_is_help=True)
drift_app = typer.Typer(help="Inspect, learn and accept response-shape baselines.", no_args_is_help=True)
app.add_typer(import_app, name="import")
app.add_typer(drift_app, name="drift")

console = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to api-qovrix.yaml (default: search upwards).")
]
EnvOpt = Annotated[str | None, typer.Option("--env", "-e", help="Environment from api-qovrix.yaml.")]
EnvFileOpt = Annotated[
    Path | None, typer.Option("--env-file", help="Load environment variables from a KEY=VALUE file.")
]


def _fail(message: str, code: int = 2) -> typer.Exit:
    err.print(f"[red]error:[/red] {escape(message)}")
    return typer.Exit(code)


def _load(config: Path | None, env: str | None, env_file: Path | None) -> Project:
    try:
        return load_project(config, env, env_file)
    except QovrixError as exc:
        raise _fail(str(exc)) from None


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"api-qovrix {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    pass


# --------------------------------------------------------------------------- init / validate


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to create the project.")] = Path("."),
    project: Annotated[str | None, typer.Option(help="Project name.")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Create api-qovrix.yaml and an example monitor."""
    from api_qovrix.scaffold import init_project

    try:
        created = init_project(directory, project, force)
    except QovrixError as exc:
        raise _fail(str(exc)) from None
    for p in created:
        console.print(f"[green]created[/green] {escape(str(p))}")
    console.print("\nNext: [bold]api-qovrix run[/bold]")


@app.command()
def validate(
    config: ConfigOpt = None,
    env: EnvOpt = None,
    env_file: EnvFileOpt = None,
    strict: Annotated[bool, typer.Option(help="Treat missing secrets as errors.")] = False,
) -> None:
    """Check config and monitor files: schema, references and secrets."""
    project = _load(config, env, env_file)
    missing = project.secrets.missing()
    used: set[str] = set()
    for m in project.monitors:
        used |= references(m.monitor.model_dump(by_alias=True), "secrets")
        if m.auth:
            used |= references(m.auth.model_dump(by_alias=True), "secrets")
    undeclared = sorted(used - set(project.secrets))
    if undeclared:
        raise _fail(f"secret(s) used but not declared under 'secrets:': {', '.join(undeclared)}")

    if project.config.openapi:
        from api_qovrix.openapi.spec import OpenAPISpec

        try:
            OpenAPISpec.load(project.config.openapi.source, project.root)
        except QovrixError as exc:
            raise _fail(str(exc)) from None

    services = sorted({m.service for m in project.monitors})
    console.print(
        f"[green]✔[/green] {len(project.monitors)} monitors in {len(services)} services"
        + (f" · env {escape(project.environment)}" if project.environment else "")
    )
    problems = 0
    for name, source in sorted(missing.items()):
        if name not in used:
            continue
        hint = f" (set {source})" if source else ""
        style = "red" if strict else "yellow"
        console.print(f"[{style}]![/{style}] secret {name} has no value{escape(hint)}")
        problems += 1
    if strict and problems:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- run


def _execute(
    project: Project,
    names: list[str],
    services: list[str],
    tags: list[str],
    concurrency: int,
    mode: BaselineMode,
) -> Any:
    try:
        monitors = select_monitors(project, names, services, tags)
    except QovrixError as exc:
        raise _fail(str(exc)) from None
    if not monitors:
        raise _fail("no monitors selected", 1)
    try:
        return asyncio.run(
            run_monitors(project, monitors, RunOptions(concurrency=concurrency, baseline_mode=mode))
        )
    except QovrixError as exc:
        raise _fail(str(exc)) from None


@app.command()
def run(
    monitors: Annotated[list[str] | None, typer.Argument(help="Only these monitors.")] = None,
    service: Annotated[
        list[str] | None, typer.Option("--service", "-s", help="Only this service (repeatable).")
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Only monitors with this tag (repeatable).")
    ] = None,
    config: ConfigOpt = None,
    env: EnvOpt = None,
    env_file: EnvFileOpt = None,
    junit: Annotated[Path | None, typer.Option(help="Write a JUnit XML report.")] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json-out", help="Write a JSON report to this file ('-' for stdout).")
    ] = None,
    update_baselines: Annotated[
        bool,
        typer.Option(
            "--update-baselines", help="Record responses into .api-qovrix/baselines/ (accepts changes)."
        ),
    ] = False,
    concurrency: Annotated[int, typer.Option(min=1, max=200, help="Monitors run in parallel.")] = 10,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every assertion and timings.")
    ] = False,
) -> None:
    """Run monitors once. Exit code 1 when any monitor fails (for CI)."""
    project = _load(config, env, env_file)
    summary = _execute(
        project,
        monitors or [],
        service or [],
        tag or [],
        concurrency,
        "update" if update_baselines else "check",
    )
    to_stdout = json_out is not None and str(json_out) == "-"
    if not to_stdout:
        print_summary(console, summary, verbose)
    if json_out is not None:
        if to_stdout:
            sys.stdout.write(to_json(summary) + "\n")
        else:
            json_out.write_text(to_json(summary) + "\n", encoding="utf-8")
    if junit is not None:
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(to_junit(summary), encoding="utf-8")
    if update_baselines and not to_stdout:
        console.print("[dim]Baselines updated in .api-qovrix/baselines/ — review and commit them.[/dim]")
    raise typer.Exit(0 if summary.ok else 1)


# --------------------------------------------------------------------------- drift


@drift_app.command("show")
def drift_show(
    monitor: Annotated[str, typer.Argument(help="Monitor name.")],
    live: Annotated[
        bool, typer.Option(help="Call the API now and show changes against the baseline.")
    ] = False,
    config: ConfigOpt = None,
    env: EnvOpt = None,
    env_file: EnvFileOpt = None,
) -> None:
    """Show a monitor's learned response shape (and, with --live, current drift)."""
    project = _load(config, env, env_file)
    try:
        project.monitor(monitor)
    except QovrixError as exc:
        raise _fail(str(exc)) from None
    data = BaselineStore(project.baselines_dir).load(monitor)
    if not data:
        console.print(
            f"No baseline for {monitor} yet. Create one: api-qovrix run {monitor} --update-baselines"
        )
        raise typer.Exit(0)
    for step_name, entry in data.get("steps", {}).items():
        status = "accepted" if entry.get("accepted") else "learning"
        console.print(
            f"[bold]{escape(monitor)}[/bold] step [bold]{escape(step_name)}[/bold]"
            f" · {entry['samples']} samples · "
            f"{status} · updated {entry.get('updated_at', '?')}"
        )
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("path")
        table.add_column("type")
        table.add_column("presence")
        for path, types, presence in _flatten(entry["shape"]):
            table.add_row(escape(path), types, presence)
        console.print(table)
    if live:
        summary = _execute(project, [monitor], [], [], 1, "check")
        console.print()
        print_summary(console, summary, verbose=False)


def _flatten(node: dict[str, Any], path: str = "$") -> list[tuple[str, str, str]]:
    rows = [(path, "|".join(sorted(node.get("types", {}))), "")]
    objects = node.get("objects", 0)
    for name, prop in sorted(node.get("props", {}).items()):
        child = f"{path}.{name}"
        sub = _flatten(prop["node"], child)
        presence = "always" if objects and prop["n"] == objects else f"{prop['n']}/{objects}"
        sub[0] = (sub[0][0], sub[0][1], presence)
        rows.extend(sub)
    if "items" in node and node["items"].get("types"):
        rows.extend(_flatten(node["items"], path + "[*]"))
    values = node.get("values")
    if isinstance(values, list) and values and node.get("strings", 0) >= 10:
        p, t, pres = rows[0]
        rows[0] = (p, f"{t} (enum, {len(values)} values)", pres)
    return rows


@drift_app.command("accept")
def drift_accept(
    monitors: Annotated[list[str], typer.Argument(help="Monitor name(s).")],
    config: ConfigOpt = None,
    env: EnvOpt = None,
    env_file: EnvFileOpt = None,
) -> None:
    """Call the API now and make the current response shape the new baseline."""
    project = _load(config, env, env_file)
    summary = _execute(project, monitors, [], [], 4, "replace")
    print_summary(console, summary)
    console.print("[green]Baseline replaced.[/green] Review and commit .api-qovrix/baselines/")
    raise typer.Exit(0 if all(r.status != "error" for r in summary.results) else 1)


@drift_app.command("learn")
def drift_learn(
    monitors: Annotated[
        list[str] | None, typer.Argument(help="Monitor name(s) (default: all with drift).")
    ] = None,
    samples: Annotated[int, typer.Option(min=1, max=100, help="How many times to call each monitor.")] = 5,
    delay: Annotated[float, typer.Option(min=0, help="Seconds between rounds.")] = 1.0,
    config: ConfigOpt = None,
    env: EnvOpt = None,
    env_file: EnvFileOpt = None,
) -> None:
    """Collect several samples per monitor so optional fields are recognised (safe GET/HEAD monitors only)."""
    project = _load(config, env, env_file)
    try:
        selected = select_monitors(project, monitors or [])
    except QovrixError as exc:
        raise _fail(str(exc)) from None
    selected = [m for m in selected if m.drift is not None or any(s.drift for s in m.steps)]
    unsafe = [m.name for m in selected if any(s.request.method not in ("GET", "HEAD") for s in m.steps)]
    if unsafe:
        console.print(f"[yellow]skipping monitors with non-GET steps:[/yellow] {', '.join(unsafe)}")
    selected = [m for m in selected if m.name not in unsafe]
    if not selected:
        raise _fail("no drift-enabled GET monitors selected", 1)

    async def rounds() -> None:
        for i in range(samples):
            summary = await run_monitors(project, selected, RunOptions(baseline_mode="learn"))
            errors = [r.name for r in summary.results if r.status == "error"]
            console.print(
                f"round {i + 1}/{samples}" + (f" [red](errors: {', '.join(errors)})[/red]" if errors else "")
            )
            if i + 1 < samples:
                await asyncio.sleep(delay)

    try:
        asyncio.run(rounds())
    except QovrixError as exc:
        raise _fail(str(exc)) from None
    console.print("[green]Done.[/green] Review and commit .api-qovrix/baselines/")


# --------------------------------------------------------------------------- import


@import_app.command("openapi")
def import_openapi(
    source: Annotated[str, typer.Argument(help="OpenAPI file or URL.")],
    service: Annotated[str | None, typer.Option(help="Service name (default: the spec title).")] = None,
    output: Annotated[
        Path | None, typer.Option(help="Monitor file to write (default: monitors/<service>.yaml).")
    ] = None,
    base_url: Annotated[str | None, typer.Option(help="Override the server URL from the spec.")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite the monitor file if it exists.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Generate monitors for every safe (GET/HEAD) operation and enable response validation."""
    from api_qovrix.importers.openapi import generate
    from api_qovrix.openapi.spec import OpenAPISpec

    try:
        config_path = find_config(config)
        root = config_path.parent
        local = (
            (root / source)
            if not source.startswith(("http://", "https://")) and not Path(source).is_absolute()
            else None
        )
        if local is not None and not local.is_file() and Path(source).is_file():
            local = Path(source).resolve()
        spec = OpenAPISpec.load(str(local) if local else source)
        result = generate(spec, service, base_url)
    except QovrixError as exc:
        raise _fail(str(exc)) from None

    out = output or root / "monitors" / f"{result.service}.yaml"
    content: dict[str, Any] = {}
    if result.service_block:
        content["service"] = result.service_block
    content["monitors"] = result.monitors

    if out.exists() and not force:
        import yaml

        existing = yaml.safe_load(out.read_text(encoding="utf-8")) or {}
        old = {str(m["name"]) for m in existing.get("monitors", []) if isinstance(m, dict) and m.get("name")}
        new = {m["name"] for m in result.monitors}
        console.print(
            f"[yellow]{escape(str(out))} exists.[/yellow] Proposed changes (use --force to overwrite):"
        )
        for name in sorted(new - old):
            console.print(f"  [green]+ {escape(name)}[/green] new operation")
        for name in sorted(old - new):
            console.print(f"  [red]- {escape(name)}[/red] no longer in the spec")
        if new == old:
            console.print("  no added or removed operations")
        raise typer.Exit(0)

    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"Generated by api-qovrix from {source} ({spec.title}).\n"
        "Safe methods only (GET/HEAD). Edit freely; re-run the import to see new or removed operations."
    )
    out.write_text(dump_yaml(content, header), encoding="utf-8")
    console.print(f"[green]wrote[/green] {escape(str(out))}: {len(result.monitors)} monitors")
    if result.skipped_unsafe:
        console.print(f"[dim]skipped {result.skipped_unsafe} unsafe operations (POST/PUT/PATCH/DELETE)[/dim]")
    rel = source
    if local is not None:
        try:
            rel = str(Path(local).resolve().relative_to(root.resolve()))
        except ValueError:
            rel = str(local)
    try:
        added = add_secrets(config_path, result.secrets)
        enabled = set_openapi_source(config_path, rel)
    except (QovrixError, TypeError, ValueError) as exc:
        raise _fail(f"wrote {out}, but could not update {config_path.name}: {exc}") from None
    for name in added:
        console.print(
            f"[green]added secret[/green] {name} to {config_path.name}: set the environment variable {name}"
        )
    if enabled:
        console.print(f"[green]enabled[/green] response validation against {escape(rel)}")
    for note in result.notes:
        console.print(f"[yellow]![/yellow] {escape(note)}")


# --------------------------------------------------------------------------- schema


@app.command(hidden=True)
def schema(
    output: Annotated[Path | None, typer.Option(help="Write schema files to this folder.")] = None,
) -> None:
    """Print or write the JSON Schema for api-qovrix.yaml and monitor files."""
    from api_qovrix.scaffold import json_schemas, write_schemas

    if output:
        for p in write_schemas(output):
            console.print(f"[green]wrote[/green] {escape(str(p))}")
    else:
        sys.stdout.write(json.dumps(json_schemas(), indent=2, sort_keys=True) + "\n")


def _utf8_output() -> None:
    """Windows uses a legacy code page (e.g. cp1252) for piped output, which can't encode ✔ or →."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _utf8_output()
    try:
        app()
    except KeyboardInterrupt:
        err.print("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
