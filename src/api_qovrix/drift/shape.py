"""Learn the shape of JSON responses and compare new responses with it.

A shape node (stored as plain JSON in .api-qovrix/baselines/) records, for one position in the document:
  types         how often each JSON type was seen there
  objects       how many objects were seen (to know which fields are always present)
  props         per field: how often it was present, and its own node
  arrays        / empty_arrays: how often an array was seen, and how often it was empty
  items         node for array items (all items are merged)
  strings       how many strings were seen
  values        short one-way hashes of the distinct string values, while there are few of them (to
                detect new enum values); null once there are too many, or when the field is sensitive

Response values themselves are never written to a baseline: only types, presence counts and hashes.
Sensitive fields (passwords, tokens, emails, ...) don't even get hashes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from typing import Any

from api_qovrix.engine.assertions import json_type
from api_qovrix.engine.results import SEVERITY_ORDER, DriftChange, Severity

MAX_ENUM_VALUES = 8
MAX_VALUE_LENGTH = 40
ENUM_MIN_OBSERVATIONS = 10
MAX_ITEMS = 200

DEFAULT_SENSITIVE = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "cookie", "session",
    "email", "phone", "ssn", "card", "iban", "address", "name",
)  # fmt: skip

Node = dict[str, Any]


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def new_node() -> Node:
    return {"types": {}}


def _key_path(path: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return f"{path}.{key}"
    escaped = key.replace("'", "\\'")
    return f"{path}['{escaped}']"


def make_sensitive_check(extra: Iterable[str] = ()) -> Callable[[str], bool]:
    words = [w.lower().replace("-", "_") for w in (*DEFAULT_SENSITIVE, *extra)]

    def is_sensitive(key: str) -> bool:
        k = key.lower().replace("-", "_")
        return any(w in k for w in words)

    return is_sensitive


def compile_ignore(patterns: Iterable[str]) -> Callable[[str], bool]:
    """'$.meta.request_id', '$..updated_at', '$.items[*].etag', '$.data.*.id' (also ignores children)."""
    regexes = []
    for pattern in patterns:
        p = pattern.strip()
        if not p.startswith("$"):
            p = "$." + p
        p = re.sub(r"\[\d+\]", "[*]", p)
        rx = re.escape(p)
        rx = rx.replace(r"\.\.", r"(?:\.|.*[.\]])")
        rx = re.sub(r"(?<!\[)\\\*(?!\\\])", lambda _: r"[^.\[]+", rx)
        regexes.append(re.compile(rf"^{rx}(?:[.\[].*)?$"))

    def ignored(path: str) -> bool:
        return any(r.match(path) for r in regexes)

    return ignored


def merge(node: Node, value: Any, *, sensitive: Callable[[str], bool], key: str = "") -> Node:
    """Add one observed value to the shape (in place) and return it."""
    t = json_type(value)
    node["types"][t] = node["types"].get(t, 0) + 1

    if t == "object":
        node["objects"] = node.get("objects", 0) + 1
        props = node.setdefault("props", {})
        for k, v in value.items():
            prop = props.setdefault(k, {"n": 0, "node": new_node()})
            prop["n"] += 1
            merge(prop["node"], v, sensitive=sensitive, key=k)
    elif t == "array":
        node["arrays"] = node.get("arrays", 0) + 1
        if not value:
            node["empty_arrays"] = node.get("empty_arrays", 0) + 1
        items = node.setdefault("items", new_node())
        for item in value[:MAX_ITEMS]:
            merge(items, item, sensitive=sensitive, key=key)
    elif t == "string":
        node["strings"] = node.get("strings", 0) + 1
        if "values" not in node:
            node["values"] = None if (key and sensitive(key)) else []
        values = node["values"]
        if values is not None:
            if len(value) > MAX_VALUE_LENGTH:
                node["values"] = None
            elif (h := value_hash(value)) not in values:
                values.append(h)
                if len(values) > MAX_ENUM_VALUES:
                    node["values"] = None
                else:
                    values.sort()
    return node


def infer(samples: Iterable[Any], *, sensitive: Callable[[str], bool] | None = None) -> Node:
    check = sensitive or make_sensitive_check()
    node = new_node()
    for s in samples:
        merge(node, s, sensitive=check)
    return node


def _fmt_types(types: dict[str, int]) -> str:
    return "|".join(sorted(types))


def compare(
    node: Node,
    value: Any,
    *,
    ignored: Callable[[str], bool] = lambda p: False,
    sensitive: Callable[[str], bool] | None = None,
    path: str = "$",
    key: str = "",
) -> list[DriftChange]:
    """Changes between the learned shape and one new value. Duplicate paths are reported once."""
    check = sensitive or make_sensitive_check()
    out: dict[tuple[str, str], DriftChange] = {}

    def add(p: str, severity: Severity, message: str) -> None:
        out.setdefault((p, message), DriftChange(p, severity, message))

    def walk(n: Node, v: Any, p: str, k: str) -> None:
        if ignored(p):
            return
        known: dict[str, int] = n.get("types", {})
        if not known:
            return
        t = json_type(v)
        if t not in known:
            if t == "integer" and "number" in known:
                pass
            elif t == "number" and "integer" in known:
                add(p, "warning", "integer → number")
                return
            else:
                add(p, "breaking", f"{_fmt_types(known)} → {t}")
                return

        if t == "object":
            objects = n.get("objects", 0)
            props: dict[str, Any] = n.get("props", {})
            for name, prop in props.items():
                child = _key_path(p, name)
                if name not in v and objects and prop["n"] == objects and not ignored(child):
                    add(child, "breaking", f"field removed (was {_fmt_types(prop['node']['types'])})")
            for name, child_value in v.items():
                child = _key_path(p, name)
                if name not in props:
                    if not ignored(child):
                        add(child, "info", f"new field ({json_type(child_value)})")
                else:
                    walk(props[name]["node"], child_value, child, name)
        elif t == "array":
            items = n.get("items")
            if not v:
                if n.get("arrays", 0) and not n.get("empty_arrays") and items and items.get("types"):
                    add(p, "warning", "array is empty (never empty before)")
                return
            if items is not None:
                for item in v[:MAX_ITEMS]:
                    walk(items, item, p + "[*]", k)
        elif t == "string":
            values = n.get("values")
            if (
                isinstance(values, list)
                and values
                and n.get("strings", 0) >= ENUM_MIN_OBSERVATIONS
                and value_hash(v) not in values
                and not (k and check(k))
                and len(v) <= MAX_VALUE_LENGTH
            ):
                add(p, "warning", f"new value {v!r} (only {len(values)} distinct values seen before)")

    walk(node, value, path, key)
    return sorted(out.values(), key=lambda c: (-SEVERITY_ORDER[c.severity], c.path))
