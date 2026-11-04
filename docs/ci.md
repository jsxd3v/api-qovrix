# CI, Docker and reports

## GitHub Actions

```yaml
- uses: actions/checkout@v4
- uses: jsxd3v/api-qovrix@v0
  with:
    working-directory: monitoring   # folder with api-qovrix.yaml (default: .)
    env: staging
    service: payments email         # optional, space-separated
    tag: smoke                      # optional
    junit: api-qovrix-results.xml   # default
    args: --concurrency 20          # optional extra arguments
  env:
    API_TOKEN: ${{ secrets.API_TOKEN }}
```

Run on a schedule for lightweight production monitoring until server mode (v0.2) is available:

```yaml
on:
  schedule:
    - cron: "*/15 * * * *"
```

## Docker

```bash
docker run --rm -v "$PWD:/work" -e API_TOKEN ghcr.io/jsxd3v/api-qovrix run --env staging
```

The image runs as a non-root user. To let it write baselines into a mounted folder on Linux, run it as
your user: `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" ghcr.io/jsxd3v/api-qovrix run --update-baselines`.

## Other CI systems

Install the binary (or use the Docker image) and run:

```bash
api-qovrix run --env staging --junit results.xml
```

## Reports

| Option | Output |
|---|---|
| (default) | Terminal summary grouped by service, with drift diffs |
| `-v` | Every assertion and the timing breakdown (connect, TLS, time to first byte, total) |
| `--junit results.xml` | JUnit XML: one test suite per service, one test case per monitor |
| `--json-out report.json` | JSON report (`--json-out -` prints it to stdout) |

Response bodies are never included in reports, and secret values are masked.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All selected monitors passed (warnings allowed) |
| 1 | At least one monitor failed or errored |
| 2 | Configuration or usage error |
