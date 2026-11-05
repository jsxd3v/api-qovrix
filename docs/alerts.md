# Alerts

api-qovrix can send an alert by **email** or **webhook** when monitors fail.

- `api-qovrix run --notify` sends alerts at the end of a run. Use it in scheduled CI jobs to get alerts
  without running a server.
- `api-qovrix serve` (v0.2) will send them continuously, with recovery messages.

## Configure notifiers

```yaml
# api-qovrix.yaml
notifiers:
  team-email:
    type: email
    to: [dev-team@example.com]
    smtp_url: ${env:SMTP_URL}     # contains a password: always from an environment variable
    from: alerts@example.com      # optional (default: the SMTP user)
    subject_prefix: "[api-qovrix]"   # optional

  team-chat:
    type: webhook
    url: ${env:CHAT_WEBHOOK_URL}  # webhook URLs usually contain a secret token

defaults:
  notify: [team-email, team-chat]  # every monitor alerts both, unless it sets its own
```

Pick notifiers per monitor or per service with `notify:` (most specific wins: monitor, then service, then
`defaults`). `notify: []` turns alerts off for one monitor.

## Send alerts

```bash
api-qovrix notifiers list            # what's configured, and which monitors use each notifier
api-qovrix notifiers test            # send a test alert to every notifier
api-qovrix notifiers test team-chat  # ...or to one
api-qovrix run --notify              # run, and alert if anything fails
```

Each notifier gets **one message per run**, listing only the failed monitors routed to it. When everything
passes, nothing is sent. A failing alert never changes the run's exit code; it's reported in the output.

The message includes each failed monitor, the reason, the breaking drift changes and, in GitHub Actions, a
link to the run (other CI systems: set `API_QOVRIX_RUN_URL`).

```text
❌ api-qovrix: 2 of 12 monitors failed (shop-api · production)

• get-order [orders] fail: drift (breaking)
    $.total  integer → string (breaking)
• checkout-flow [orders] error: create-cart: connection failed

Run: https://github.com/acme/shop/actions/runs/123
```

## Email

`smtp_url` works with any SMTP server or email provider:

| URL | Meaning |
|---|---|
| `smtp://user:password@host:587` | Port 587, upgraded to TLS with STARTTLS (required) |
| `smtps://user:password@host:465` | Port 465, TLS from the start |
| `smtp://host:25?insecure=true` | Plain SMTP, e.g. a local relay on a private network |

Characters like `@` in the user or password must be URL-encoded (`@` → `%40`).

## Webhook

By default the webhook receives a JSON `POST`:

```json
{
  "event": "run.failed",
  "text": "❌ api-qovrix: 2 of 12 monitors failed ...",
  "subject": "2 monitors failed · shop-api · production",
  "project": "shop-api",
  "environment": "production",
  "total": 12,
  "failed_count": 2,
  "failed": [{ "name": "get-order", "service": "orders", "status": "fail", "summary": "...", "drift": [] }],
  "run_url": "https://...",
  "timestamp": "2026-11-05T09:30:00+00:00"
}
```

Many chat tools' incoming webhooks read the `text` field, so this works as is. If yours expects another
shape, set `body`; strings in it can use `{{ alert.<field> }}`:

```yaml
notifiers:
  team-chat:
    type: webhook
    url: ${env:CHAT_WEBHOOK_URL}
    body:
      content: "{{ alert.text }}"   # a tool that expects 'content' instead of 'text'
  pager:
    type: webhook
    url: ${env:INCIDENT_WEBHOOK_URL}
    headers: { Authorization: "Bearer ${env:INCIDENT_TOKEN}" }
    body:
      summary: "{{ alert.subject }}"
      severity: critical
      details: "{{ alert.text }}"
```

`body` can also be a plain string, which is sent as `text/plain`.

## In GitHub Actions

```yaml
on:
  schedule:
    - cron: "*/15 * * * *"
jobs:
  monitors:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: jsxd3v/api-qovrix@v0
        with:
          notify: true
        env:
          CHAT_WEBHOOK_URL: ${{ secrets.CHAT_WEBHOOK_URL }}
          SMTP_URL: ${{ secrets.SMTP_URL }}
```
