"""`{{ ... }}` expressions in monitor files.

Simple lookups such as `{{ steps.create-cart.cart_id }}` are resolved directly (so names may contain `-`).
Anything else is evaluated by a sandboxed Jinja2 environment: no access to Python internals, no imports and
no file or network access, so a monitor file cannot run code.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from jinja2 import StrictUndefined, UndefinedError
from jinja2.exceptions import SecurityError, TemplateSyntaxError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from api_qovrix.errors import SecretMissingError, TemplateError

EXPR_RE = re.compile(r"\{\{\s*((?:(?!\}\}|\{\{).)*?)\s*\}\}", re.DOTALL)
SIMPLE_PATH_RE = re.compile(r"^[A-Za-z_][\w-]*(?:\.[\w-]+|\[\d+\])*$")
PATH_PART_RE = re.compile(r"\.?([\w-]+)|\[(\d+)\]")

_env = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


class Secrets(Mapping[str, str]):
    """Secret values resolved from environment variables. Missing ones fail only when used."""

    def __init__(self, values: Mapping[str, str | None], sources: Mapping[str, str] | None = None) -> None:
        self._values = dict(values)
        self._sources = dict(sources or {})

    def __getitem__(self, key: str) -> str:
        if key not in self._values:
            raise SecretMissingError(f"secret '{key}' is not declared under 'secrets:' in api-qovrix.yaml")
        value = self._values[key]
        if value is None:
            source = self._sources.get(key)
            hint = f" (set the environment variable {source})" if source else ""
            raise SecretMissingError(f"secret '{key}' has no value{hint}")
        return value

    def __iter__(self) -> Any:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def missing(self) -> dict[str, str | None]:
        return {k: self._sources.get(k) for k, v in self._values.items() if v is None}

    def known_values(self) -> list[str]:
        return [v for v in self._values.values() if v]


def _lookup(path: str, ctx: Mapping[str, Any]) -> Any:
    parts = [m.group(1) if m.group(1) is not None else int(m.group(2)) for m in PATH_PART_RE.finditer(path)]
    current: Any = ctx
    walked: list[str] = []
    for part in parts:
        try:
            if isinstance(part, int) or isinstance(current, Mapping):
                current = current[part]
            else:
                current = getattr(current, part)
        except SecretMissingError:
            raise
        except (KeyError, IndexError, TypeError, AttributeError):
            where = ".".join(str(p) for p in walked) or "context"
            raise TemplateError(f"'{path}': '{part}' not found in {where}") from None
        walked.append(str(part))
    return current


def evaluate(expr: str, ctx: Mapping[str, Any]) -> Any:
    expr = expr.strip()
    if SIMPLE_PATH_RE.match(expr):
        return _lookup(expr, ctx)
    try:
        return _env.compile_expression(expr, undefined_to_none=False)(**ctx)
    except SecretMissingError:
        raise
    except UndefinedError as exc:
        raise TemplateError(f"'{{{{ {expr} }}}}': {exc.message}") from None
    except SecurityError as exc:
        raise TemplateError(f"'{{{{ {expr} }}}}' is not allowed: {exc}") from None
    except TemplateSyntaxError as exc:
        raise TemplateError(f"'{{{{ {expr} }}}}' has a syntax error: {exc.message}") from None
    except Exception as exc:  # anything raised while evaluating user expressions
        raise TemplateError(f"'{{{{ {expr} }}}}' failed: {exc}") from None


def render_string(text: str, ctx: Mapping[str, Any], only_roots: Iterable[str] | None = None) -> Any:
    """Render `{{ ... }}` in `text`.

    A string that is exactly one expression keeps the value's type (so `{{ vars.id }}` can stay an int).
    With `only_roots`, expressions that don't start with one of those names are left untouched.
    """
    roots = set(only_roots) if only_roots is not None else None

    def wanted(expr: str) -> bool:
        if roots is None:
            return True
        head = re.split(r"[.\[\s(|]", expr.strip(), maxsplit=1)[0]
        return head in roots or any(re.search(rf"\b{re.escape(r)}\.", expr) for r in roots)

    whole = EXPR_RE.fullmatch(text.strip())
    if whole and text.strip() == text and wanted(whole.group(1)):
        return evaluate(whole.group(1), ctx)

    def repl(match: re.Match[str]) -> str:
        if not wanted(match.group(1)):
            return match.group(0)
        value = evaluate(match.group(1), ctx)
        return "" if value is None else str(value)

    return EXPR_RE.sub(repl, text)


def render_value(value: Any, ctx: Mapping[str, Any], only_roots: Iterable[str] | None = None) -> Any:
    """Render every string inside dicts and lists."""
    if isinstance(value, str):
        return render_string(value, ctx, only_roots) if "{{" in value else value
    if isinstance(value, dict):
        return {k: render_value(v, ctx, only_roots) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, ctx, only_roots) for v in value]
    return value


def references(value: Any, root: str) -> set[str]:
    """Names used as `{{ <root>.NAME ... }}` anywhere inside value (e.g. which secrets a monitor needs)."""
    found: set[str] = set()
    pattern = re.compile(rf"\b{re.escape(root)}\.([\w-]+)")

    def walk(v: Any) -> None:
        if isinstance(v, str):
            for expr in EXPR_RE.findall(v):
                found.update(pattern.findall(expr))
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(value)
    return found


Redactor = Callable[[str], str]


def make_redactor(secret_values: Iterable[str]) -> Redactor:
    """Return a function that masks known secret values in text (for logs, reports and errors)."""
    values = sorted({v for v in secret_values if v and len(v) >= 4}, key=len, reverse=True)

    def redact(text: str) -> str:
        for v in values:
            text = text.replace(v, "***")
        return text

    return redact
