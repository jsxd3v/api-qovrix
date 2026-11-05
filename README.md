# api-qovrix

**Self-hosted API monitoring for any stack that catches breaking changes, not just downtime.**
Monitors live in Git and run the same in CI and production.

[![CI](https://github.com/jsxd3v/api-qovrix/actions/workflows/ci.yml/badge.svg)](https://github.com/jsxd3v/api-qovrix/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)

Uptime tools tell you *whether* an API answers. api-qovrix also tells you whether it still answers
**the way your code expects**: a field renamed, a number that became a string, a value that is suddenly
`null`. Those changes return `200 OK`, so most monitors stay green while your clients break.

```text
$ api-qovrix run
shop-api
  ✔  list-orders     200   84 ms
  ✘  get-order       200   91 ms  → drift (breaking)
  get-order
    DRIFT (breaking)
      ✘ $.customer.email   string → null
      ✘ $.total            integer → string
      + $.total_cents      new field (integer)   info

1 passed, 1 failed · 0.3s · env production
```

## Works with any stack

api-qovrix only talks HTTP, so your API can be written in any language: JavaScript/TypeScript, PHP, Go,
Java, C#, Ruby or Python. Monitors are YAML files; **you don't need Python** unless you choose to install with pip.

| You use | Install |
|---|---|
| Any laptop (macOS, Linux) | `curl -fsSL https://github.com/jsxd3v/api-qovrix/releases/latest/download/install.sh \| sh` |
| Windows | Download `api-qovrix-windows-x64.exe` from [Releases](https://github.com/jsxd3v/api-qovrix/releases) |
| Docker | `docker run --rm -v "$PWD:/work" ghcr.io/jsxd3v/api-qovrix run` |
| GitHub Actions | `uses: jsxd3v/api-qovrix@v0` ([see below](#in-ci)) |
| Python | `pip install api-qovrix` |

## Quickstart

```bash
api-qovrix init            # creates api-qovrix.yaml and monitors/example.yaml
api-qovrix run             # runs every monitor once; exit code 1 if anything fails
```

Generate monitors from your OpenAPI spec (optional):

```bash
api-qovrix import openapi ./openapi.yaml
```

## What a monitor looks like

```yaml
# monitors/orders.yaml
monitors:
  - name: get-order
    request:
      method: GET
      url: /v1/orders/{{ vars.order_id }}
      auth: { type: bearer, token: "{{ secrets.API_TOKEN }}" }
    vars: { order_id: 1001 }
    assert:
      - status: 200
      - response_time_ms: { lt: 800 }
      - jsonpath: $.status
        in: [pending, paid, shipped]
    drift:
      enabled: true
      ignore: ["$.meta.request_id"]

  - name: checkout-flow
    steps:
      - name: login
        request: { method: POST, url: /v1/auth/token, json: { client_id: "{{ secrets.CLIENT_ID }}" } }
        extract: { token: $.access_token }
      - name: create-cart
        request:
          method: POST
          url: /v1/carts
          headers: { Authorization: "Bearer {{ steps.login.token }}" }
        extract: { cart_id: $.id }
        assert: [{ status: 201 }]
      - name: cleanup
        always_run: true
        request: { method: DELETE, url: "/v1/carts/{{ steps.create-cart.cart_id }}" }
```

Editors autocomplete these files from the published [JSON Schema](schema/).

## Features

**Available now (v0.1)**

- **Contract drift detection.** api-qovrix learns the shape of each response and reports removed fields,
  type changes, new `null`s, unseen enum values and arrays that are suddenly empty, with a readable diff.
  New fields are `info`, optional fields are recognised, and volatile paths can be ignored, so it stays quiet
  when nothing important changed.
- **Baselines in Git, like snapshot tests.** Shapes are stored in `.api-qovrix/baselines/`, reviewed in pull
  requests, and updated with `--update-baselines` or `api-qovrix drift accept`. They contain types and counts
  only: response values are never written to them.
- **OpenAPI validation.** Every response is checked against your spec (3.0 and 3.1). `import openapi`
  generates monitors for safe (GET/HEAD) operations.
- **Multi-step flows** with values passed between steps, cleanup steps that always run, and retries.
- **Auth:** bearer, basic, API key (header or query) and OAuth2 client credentials with token caching.
- **Assertions:** status, response time, headers, body text, JSON Schema and JSONPath with `equals`, `in`,
  `contains`, `matches`, `gt`/`lt`, `length`, `type`, `exists`, `not_empty` and more.
- **Services:** monitors grouped per API, with shared base URL, auth, headers and links to the provider's
  status page.
- **Continuous monitoring:** `api-qovrix serve` runs each monitor on its interval, keeps history in SQLite,
  and alerts when a monitor goes down (after N failures in a row) or recovers, grouped per service.
- **Alerts by email or webhook:** `api-qovrix run --notify` sends one message per notifier listing what
  failed and why, including drift diffs. Works from scheduled CI, no server needed.
- **CI-ready:** exit codes, JUnit XML, JSON reports, a GitHub Action, Docker image and standalone binaries.
- **Secrets stay secret:** only read from environment variables and masked in every report.

**Coming soon**

- **v0.2 · Dashboard:** a web page with the status of every service and monitor, history and drift diffs,
  plus a REST API, both password-protected.
- **v0.3 · Insight:** learned latency baselines, `Deprecation` / `Sunset` / rate-limit / TLS-expiry warnings,
  status page, metrics export, more storage and install options, Python plugins.

## In CI

```yaml
# .github/workflows/api-monitors.yml
on:
  push:
  schedule:
    - cron: "*/15 * * * *"
jobs:
  monitors:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: jsxd3v/api-qovrix@v0
        with:
          env: staging
        env:
          API_TOKEN: ${{ secrets.API_TOKEN }}
```

Any other CI works too: run the binary or `docker run --rm -v "$PWD:/work" ghcr.io/jsxd3v/api-qovrix run --junit results.xml`.

## Documentation

- [Getting started](docs/getting-started.md)
- [Configuration: environments, secrets, services](docs/configuration.md)
- [Monitors, flows, auth and assertions](docs/monitors.md)
- [Drift detection and baselines](docs/drift.md)
- [OpenAPI](docs/openapi.md)
- [Continuous monitoring (`serve`)](docs/serve.md)
- [Alerts: email and webhook](docs/alerts.md)
- [CI, Docker and reports](docs/ci.md)
- [CLI reference](docs/cli.md)

A runnable example lives in [`examples/public-api`](examples/public-api).

## Contributing

Bug reports, ideas and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE). Copyright 2026 jsxd3v.
