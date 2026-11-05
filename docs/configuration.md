# Configuration

## api-qovrix.yaml

```yaml
version: 1
project: shop-api

defaults:              # used when nothing more specific is set
  interval: 1m         # used by `serve` (v0.2)
  timeout: 10s
  retries: 1
  headers: { Accept: application/json }
  drift: { enabled: true }   # turn on drift for every monitor

environments:          # pick with --env or API_QOVRIX_ENV; default: default_environment or the first one
  staging:
    base_url: https://staging.api.example.com
  production:
    base_url: https://api.example.com
    vars: { order_id: 1001 }
default_environment: production

secrets:               # values come from environment variables only
  API_TOKEN: ${env:API_TOKEN}
  PAYMENTS_API_KEY: ${env:PAYMENTS_API_KEY}

services:              # shared settings per API (see below)
  payments:
    base_url: https://payments.example.com
    auth: { type: bearer, token: "{{ secrets.PAYMENTS_API_KEY }}" }
    links: { status: https://status.payments.example.com }

openapi:               # validate responses against a spec
  source: ./openapi.yaml

include:               # where monitor files live (default shown)
  - monitors/**/*.yaml

server:
  public_url: https://monitor.example.com   # links in alerts; more `server` settings in serve.md

branding: { name: Acme, logo: branding/logo.svg, accent: "#0e7490" }   # see dashboard.md
status_page: { enabled: false }                                       # see status-page.md

redact: [customer_ref] # extra field names to treat as sensitive
allowed_hosts:         # optional: only allow these hosts (wildcards allowed)
  - "*.example.com"
```

Durations accept `500ms`, `10s`, `1m`, `2h` or a number of seconds.

## Secrets

Secrets are never written in a file. Each one maps a name to an environment variable:

```yaml
secrets:
  API_TOKEN: ${env:API_TOKEN}
```

Use them in monitors as `{{ secrets.API_TOKEN }}`. A missing variable only fails the monitors that use it, and
`api-qovrix validate` lists missing ones (`--strict` turns them into errors). For local runs you can keep
variables in a file that is not committed: `api-qovrix run --env-file .env`.

Secret values (and OAuth2 tokens obtained during a run) are replaced with `***` in every report.

## Services

A service is one API: yours or a third party. Each monitor belongs to one, set with `service:` or taken from
the monitor file's name (`monitors/payments.yaml` → `payments`). Settings can live in `api-qovrix.yaml` under
`services:`, or at the top of a monitor file:

```yaml
# monitors/payments.yaml
service:
  base_url: https://payments.example.com
  auth: { type: bearer, token: "{{ secrets.PAYMENTS_API_KEY }}" }
monitors:
  - name: payments-balance
    request: { url: /v1/balance }
```

Defining the same service in both places is an error. Run one service with `api-qovrix run --service payments`.

## Which setting wins

For `base_url`, `auth`, `headers`, `timeout`, `retries`, `interval` and `vars`:

**step request → monitor → service → environment → defaults**

Headers and vars are merged, with the more specific value winning. A full URL in `request.url` ignores
`base_url`.

## Templating

Strings can use `{{ ... }}`:

| Expression | Value |
|---|---|
| `{{ secrets.NAME }}` | a secret |
| `{{ vars.name }}` | a variable from environment, service or monitor `vars` |
| `{{ steps.step-name.value }}` | a value extracted by an earlier step |
| `{{ env.name }}` | the selected environment's name |

A string that is only one expression keeps the value's type (numbers stay numbers in JSON bodies).
Expressions run in a sandbox: no imports, no file or network access, no Python internals.
