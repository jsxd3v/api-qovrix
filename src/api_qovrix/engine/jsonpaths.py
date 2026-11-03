"""JSONPath helpers built on jsonpath-ng (extended syntax: filters, len, etc.)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from jsonpath_ng.exceptions import JsonPathParserError
from jsonpath_ng.ext.parser import parse

from api_qovrix.errors import ConfigError


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


MISSING: Any = _Missing()


@lru_cache(maxsize=512)
def compile_path(expr: str) -> Any:
    try:
        return parse(expr)
    except (JsonPathParserError, Exception) as exc:
        raise ConfigError(f"invalid JSONPath {expr!r}: {exc}") from None


def query(data: Any, expr: str) -> Any:
    """One match -> the value; several -> a list; none -> MISSING.

    A path with a wildcard or filter ([*], [?...], ..) returns a list when it matches anything.
    """
    matches = [m.value for m in compile_path(expr).find(data)]
    if not matches:
        return MISSING
    multi = any(token in expr for token in ("[*]", "[?", "..", "*"))
    if multi:
        return matches
    return matches[0] if len(matches) == 1 else matches
