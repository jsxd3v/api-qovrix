# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-05

Second release: alerts, continuous monitoring and the dashboard.

### Added
- Alerts by email (any SMTP server) and webhook (JSON POST, custom body template).
- `api-qovrix run --notify`: one alert per notifier listing the failed monitors, drift changes and the CI
  run link.
- `api-qovrix notifiers list` and `api-qovrix notifiers test`.
- `validate` warns when a notifier's URL is not set; the GitHub Action has a `notify` input.
- `api-qovrix serve`: runs monitors on their intervals, stores history in SQLite (with retention), and sends
  alerts when a monitor is down after `alert_after` failures in a row, optional reminders (`alert_repeat`) and
  recovery messages, grouped per service (`server.group_wait`). `--once` and JSON logs.
- `api-qovrix services` and `api-qovrix history`.
- Dashboard served by `serve` (default `http://localhost:8080`): overview per service with 24 h uptime and
  response-time charts, monitor pages with run details and drift diffs, incidents, "Run now". Server-rendered,
  no external scripts, light/dark mode, works on phones.
- REST API under `/api/v1` (services, monitors, runs, incidents, run now) and `/healthz`.
- Security: password login (`API_QOVRIX_PASSWORD`) with signed HttpOnly cookies and rate limiting, API keys
  (`API_QOVRIX_API_KEY`), strict CSP; refuses to listen beyond localhost without a password.
- Docker image: history volume at `/data`, dashboard on port 8080; Docker Compose example with health check.
- `api-qovrix run --html report.html`: a self-contained HTML report (no scripts or external files); the
  GitHub Action has an `html` input.
- Hidden `api-qovrix selftest` command, used to smoke-test release binaries and the Docker image.

## [0.1.0] - 2026-11-04

First release: "CI and contracts".

### Added
- `api-qovrix run`: run monitors once, with exit codes for CI, JUnit XML and JSON reports.
- YAML monitors with single requests and multi-step flows (`extract`, `always_run` cleanup steps, retries).
- Auth: bearer, basic, API key (header or query) and OAuth2 client credentials with token caching.
- Assertions: status, response time, headers, body text, JSON Schema and JSONPath operators.
- Contract drift detection with severities, optional-field learning, ignore paths and readable diffs.
- Baselines stored in `.api-qovrix/baselines/` (no response values), with `drift learn`, `drift accept`,
  `drift show` and `run --update-baselines`.
- OpenAPI 3.0 / 3.1 response validation and `import openapi` (safe GET/HEAD operations only).
- Services: monitors grouped per API with shared settings and setting precedence.
- Secrets from environment variables only, masked in all output; optional `allowed_hosts`.
- JSON Schema for `api-qovrix.yaml` and monitor files.
- Docker image, GitHub Action, standalone binaries and an install script.

[Unreleased]: https://github.com/jsxd3v/api-qovrix/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jsxd3v/api-qovrix/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jsxd3v/api-qovrix/releases/tag/v0.1.0
