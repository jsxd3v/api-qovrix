# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - Unreleased

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
