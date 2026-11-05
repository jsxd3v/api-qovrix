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
    notify: true                    # send alerts when monitors fail (see alerts.md)
    html: api-qovrix-report.html    # optional self-contained HTML report
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
| `--html report.html` | One self-contained HTML file: summary, every monitor (failures expanded), checks and drift diffs. No scripts or external files, so it opens offline and can be kept as a CI artifact or emailed |

Response bodies are never included in reports, and secret values are masked.

Keep the HTML report with each CI run:

```yaml
- uses: jsxd3v/api-qovrix@v0
  with:
    html: api-qovrix-report.html
- uses: actions/upload-artifact@v4
  if: always()                      # keep it when monitors fail too
  with:
    name: api-qovrix-report
    path: api-qovrix-report.html
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All selected monitors passed (warnings allowed) |
| 1 | At least one monitor failed or errored |
| 2 | Configuration or usage error |
