import pytest

from api_qovrix.config.templating import Secrets, make_redactor, references, render_string, render_value
from api_qovrix.errors import SecretMissingError, TemplateError


def test_simple_lookup_with_hyphenated_names():
    ctx = {"steps": {"create-cart": {"cart_id": 42}}}
    assert render_string("/carts/{{ steps.create-cart.cart_id }}", ctx) == "/carts/42"


def test_whole_expression_keeps_type():
    assert render_string("{{ vars.id }}", {"vars": {"id": 7}}) == 7
    assert render_value({"a": ["{{ vars.flag }}"]}, {"vars": {"flag": True}}) == {"a": [True]}


def test_jinja_expression():
    ctx = {"params": {"region": "eu"}}
    expr = "{{ 'https://eu.example.com' if params.region == 'eu' else 'https://example.com' }}"
    assert render_string(expr, ctx) == "https://eu.example.com"


def test_missing_value_is_an_error():
    with pytest.raises(TemplateError, match="not found"):
        render_string("{{ vars.nope }}", {"vars": {}})


def test_sandbox_blocks_python_internals():
    with pytest.raises(TemplateError):
        render_string("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_only_roots_leaves_other_expressions():
    out = render_string(
        "{{ params.domain }}/{{ secrets.KEY }}", {"params": {"domain": "d"}}, only_roots={"params"}
    )
    assert out == "d/{{ secrets.KEY }}"


def test_secrets_missing_and_undeclared():
    s = Secrets({"A": "x", "B": None}, {"B": "ENV_B"})
    assert render_string("{{ secrets.A }}", {"secrets": s}) == "x"
    with pytest.raises(SecretMissingError, match="ENV_B"):
        render_string("{{ secrets.B }}", {"secrets": s})
    with pytest.raises(SecretMissingError, match="not declared"):
        render_string("{{ secrets.C }}", {"secrets": s})
    assert s.missing() == {"B": "ENV_B"}


def test_references_and_redactor():
    assert references({"h": "Bearer {{ secrets.TOKEN }}", "x": ["{{ secrets.B }}"]}, "secrets") == {
        "TOKEN",
        "B",
    }
    redact = make_redactor(["supersecret", "ab"])
    assert redact("token=supersecret&x=ab") == "token=***&x=ab"
