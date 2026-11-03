from api_qovrix.drift.shape import (
    compare,
    compile_ignore,
    infer,
    make_sensitive_check,
    merge,
    new_node,
    value_hash,
)

ORDER = {
    "id": 1,
    "status": "paid",
    "total": 10,
    "customer": {"email": "a@example.com", "vip": False},
    "lines": [{"sku": "x"}],
}


def kinds(changes):
    return {(c.path, c.severity) for c in changes}


def test_identical_response_has_no_changes():
    shape = infer([ORDER])
    assert compare(shape, ORDER) == []


def test_breaking_changes():
    shape = infer([ORDER])
    new = {**ORDER, "total": "10.00", "customer": {"email": None, "vip": False}}
    del new["status"]
    got = kinds(compare(shape, new))
    assert ("$.total", "breaking") in got
    assert ("$.customer.email", "breaking") in got
    assert ("$.status", "breaking") in got


def test_new_field_is_info():
    shape = infer([ORDER])
    changes = compare(shape, {**ORDER, "total_cents": 1000})
    assert kinds(changes) == {("$.total_cents", "info")}
    assert changes[0].message == "new field (integer)"


def test_integer_to_number_is_warning_and_reverse_is_fine():
    assert kinds(compare(infer([{"n": 1}]), {"n": 1.5})) == {("$.n", "warning")}
    assert compare(infer([{"n": 1.5}]), {"n": 2}) == []


def test_optional_fields_are_not_breaking():
    shape = infer([{"a": 1, "b": 2}, {"a": 1}])
    assert compare(shape, {"a": 3}) == []
    shape = infer([{"items": [{"a": 1, "coupon": "x"}, {"a": 2}]}])
    assert compare(shape, {"items": [{"a": 5}]}) == []


def test_empty_array_warning():
    shape = infer([{"items": [1, 2]}, {"items": [3]}])
    assert kinds(compare(shape, {"items": []})) == {("$.items", "warning")}
    shape = infer([{"items": [1]}, {"items": []}])
    assert compare(shape, {"items": []}) == []


def test_new_enum_value_after_enough_observations():
    shape = infer([{"orders": [{"state": s} for s in ["paid", "pending", "paid", "shipped"] * 3]}])
    changes = compare(shape, {"orders": [{"state": "refunded"}]})
    assert kinds(changes) == {("$.orders[*].state", "warning")}
    # Few observations: no enum learned (ids, names, etc. must not trigger)
    few = infer([{"id": "abc"}])
    assert compare(few, {"id": "abd"}) == []


def test_sensitive_values_are_never_stored():
    sensitive = make_sensitive_check()
    shape = new_node()
    for _ in range(12):
        merge(shape, {"email": "a@example.com", "api_token": "t", "state": "ok"}, sensitive=sensitive)
    props = shape["props"]
    assert props["email"]["node"]["values"] is None
    assert props["api_token"]["node"]["values"] is None
    assert props["state"]["node"]["values"] == [value_hash("ok")]


def test_ignore_patterns():
    ignored = compile_ignore(["$.meta.request_id", "$..updated_at", "$.items[0].etag", "$.data.*.tmp"])
    assert ignored("$.meta.request_id")
    assert ignored("$.meta.request_id.inner")
    assert not ignored("$.meta.other")
    assert ignored("$.updated_at")
    assert ignored("$.a.b[*].updated_at")
    assert not ignored("$.not_updated_at")
    assert ignored("$.items[*].etag")
    assert ignored("$.data.x.tmp")

    shape = infer([{"meta": {"request_id": "1"}, "x": 1}])
    assert compare(shape, {"meta": {"request_id": 5}, "x": 1}, ignored=ignored) == []


def test_type_change_of_root():
    assert kinds(compare(infer([{"a": 1}]), [1])) == {("$", "breaking")}


def test_baselines_never_contain_response_values():
    import json

    sample = {"street": "Kulas Light", "state": "paid", "items": [{"sku": "ABC-123"}] * 12}
    dumped = json.dumps(infer([sample] * 12))
    for raw in ("Kulas Light", "paid", "ABC-123"):
        assert raw not in dumped
