# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-11-05

Shaped by feedback from the first production setup.

### Added
- Shared login (`token_request` auth): describe a login request once on a service; all its monitors reuse
  the token until it expires, instead of signing in in every monitor.
- Cron schedules for `serve` (`cron: "0 8 * * mon-fri"`, `@hourly`, ...) with `server.timezone`.
- Monitor files reload automatically while `serve` runs (`server.watch`); only added or changed monitors
  restart, and a broken edit keeps the previous monitors running.
- Heartbeat pings (`server.heartbeat`) so an outside service can alert you when `serve` itself stops.
- `/healthz` reports the scheduler's state (`503` when it isn't running) and the last check time.
- `description` on flow steps.
- Docs: "Monitoring the monitor", cron schedules, reloading, shared login.

### Fixed
- `{{ ... }}` now works in `base_url` (service, monitor and environment), so an address can come from a
  secret or variable.
- Docs said baselines contain no response values; they hold short one-way fingerprints of enum-like values.
  The wording is now exact (README, SECURITY.md, drift docs).

## [0.2.2] - 2026-11-05

### Fixed
- Dashboard login, logout and "Run now" were refused with "cross-origin request refused" in real browsers
  when a password was set. The `no-referrer` policy made browsers send `Origin: null` on the dashboard's own
  forms; it is now `same-origin`.
- The cross-site check now uses the browser's `Sec-Fetch-Site` header, so forms also work behind a reverse
  proxy (where the public address differs from the one the server sees). Older browsers fall back to comparing
  `Origin` with `Host` or `X-Forwarded-Host`.

### Added
- CI runs the dashboard in a real browser (login, forms, stylesheet), so browser-only problems are caught.
- Docs: how to generate and store the dashboard password safely (`.env` + `--env-file`, not the command line).

## [0.2.1] - 2026-11-05

### Fixed
- Dashboard on Windows: stylesheet and script are always served with the right content type, even when the
  Windows registry maps `.js`/`.css` to `text/plain` (browsers refused them, so pages showed unstyled).

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

[Unreleased]: https://github.com/jsxd3v/api-qovrix/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jsxd3v/api-qovrix/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/jsxd3v/api-qovrix/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jsxd3v/api-qovrix/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jsxd3v/api-qovrix/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jsxd3v/api-qovrix/releases/tag/v0.1.0
