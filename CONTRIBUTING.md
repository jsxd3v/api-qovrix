# Contributing to api-qovrix

Thanks for helping! Bug reports, ideas, docs fixes and pull requests are all welcome.

## Development setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Before opening a pull request, run the same checks as CI:

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy
.venv/bin/pytest --cov
```

If you change the config models, regenerate the published JSON Schema:

```bash
.venv/bin/api-qovrix schema --output schema
```

Tests never call real APIs: HTTP is mocked with [respx](https://lundberg.github.io/respx/).

## Pull requests

- Keep changes focused; one feature or fix per pull request.
- Add or update tests and docs.
- Add a line to `CHANGELOG.md` under "Unreleased".

## Reporting security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
