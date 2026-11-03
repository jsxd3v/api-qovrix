import pytest

from api_qovrix.config.loader import load_project
from api_qovrix.errors import ConfigError

CONFIG = """
version: 1
defaults:
  timeout: 5s
  retries: 1
environments:
  staging:
    base_url: https://staging.example.com
    vars: { region: s }
  production:
    base_url: https://api.example.com
secrets:
  TOKEN: ${env:QOVRIX_TEST_TOKEN}
services:
  billing:
    base_url: https://billing.example.com
    auth: { type: bearer, token: "{{ secrets.TOKEN }}" }
    timeout: 3s
"""


def test_precedence_and_services(make_project, monkeypatch):
    monkeypatch.setenv("QOVRIX_TEST_TOKEN", "t0k3n")
    root = make_project(
        CONFIG,
        {
            "monitors/billing.yaml": """
                monitors:
                  - name: invoices
                    request: { url: /invoices }
                  - name: own-url
                    base_url: https://other.example.com
                    timeout: 1s
                    request: { url: /x }
            """,
            "monitors/shop.yaml": """
                service:
                  headers: { X-Shop: "1" }
                monitors:
                  - name: products
                    request: { url: /products }
            """,
        },
    )
    project = load_project(root / "api-qovrix.yaml")
    assert project.environment == "staging"  # first environment by default
    inv = project.monitor("invoices")
    assert inv.service == "billing"
    assert inv.base_url == "https://billing.example.com"
    assert inv.timeout == 3.0
    assert inv.retries == 1
    assert inv.auth is not None
    own = project.monitor("own-url")
    assert own.base_url == "https://other.example.com" and own.timeout == 1.0
    products = project.monitor("products")
    assert products.service == "shop"
    assert products.base_url == "https://staging.example.com"
    assert products.headers == {"X-Shop": "1"}
    assert products.vars == {"region": "s"}
    assert project.secrets["TOKEN"] == "t0k3n"


def test_environment_selection(make_project):
    root = make_project(CONFIG, {"monitors/a.yaml": "monitors:\n  - name: a\n    request: { url: /a }\n"})
    assert (
        load_project(root / "api-qovrix.yaml", "production").monitor("a").base_url
        == "https://api.example.com"
    )
    with pytest.raises(ConfigError, match="unknown environment"):
        load_project(root / "api-qovrix.yaml", "nope")


def test_service_defined_twice(make_project):
    root = make_project(CONFIG, {"monitors/billing.yaml": "service: {}\nmonitors: []\n"})
    with pytest.raises(ConfigError, match="defined twice"):
        load_project(root / "api-qovrix.yaml")


def test_duplicate_monitor_names(make_project):
    m = "monitors:\n  - name: same\n    request: { url: /a }\n"
    root = make_project(CONFIG, {"monitors/a.yaml": m, "monitors/b.yaml": m})
    with pytest.raises(ConfigError, match="used twice"):
        load_project(root / "api-qovrix.yaml")


def test_secret_values_must_come_from_env(make_project):
    root = make_project("secrets:\n  TOKEN: plain-text-value\n")
    with pytest.raises(ConfigError, match="environment variable"):
        load_project(root / "api-qovrix.yaml")


def test_invalid_monitor_reports_location(make_project):
    root = make_project(
        "version: 1\n",
        {"monitors/a.yaml": "monitors:\n  - name: a\n    request: { url: /a }\n    steps: []\n"},
    )
    with pytest.raises(ConfigError, match=r"monitors\.0"):
        load_project(root / "api-qovrix.yaml")


def test_assertion_needs_one_target(make_project):
    bad = "monitors:\n  - name: a\n    request: { url: /a }\n    assert:\n      - jsonpath: $.x\n"
    root = make_project("version: 1\n", {"monitors/a.yaml": bad})
    with pytest.raises(ConfigError, match="needs an operator"):
        load_project(root / "api-qovrix.yaml")


def test_unknown_notifier(make_project):
    m = "monitors:\n  - name: a\n    notify: [ghost]\n    request: { url: /a }\n"
    root = make_project("version: 1\n", {"monitors/a.yaml": m})
    with pytest.raises(ConfigError, match="unknown notifier"):
        load_project(root / "api-qovrix.yaml")


def test_durations(make_project):
    m = "monitors:\n  - name: a\n    interval: 2m\n    timeout: 500ms\n    request: { url: /a }\n"
    root = make_project(
        "version: 1\nenvironments: { p: { base_url: 'https://x' } }\n", {"monitors/a.yaml": m}
    )
    mon = load_project(root / "api-qovrix.yaml").monitor("a")
    assert mon.interval == 120.0 and mon.timeout == 0.5


def test_env_file(make_project, tmp_path, monkeypatch):
    monkeypatch.delenv("QOVRIX_FROM_FILE", raising=False)
    root = make_project("secrets:\n  S: ${env:QOVRIX_FROM_FILE}\n")
    env_file = tmp_path / "local.env"
    env_file.write_text('# comment\nexport QOVRIX_FROM_FILE="abc"\n')
    project = load_project(root / "api-qovrix.yaml", env_file=env_file)
    assert project.secrets["S"] == "abc"
