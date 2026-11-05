# Continuous monitoring (`serve`)

`api-qovrix run` checks once and exits, which suits CI. `api-qovrix serve` keeps running: it checks each
monitor on its `interval`, stores the history, and sends alerts when something goes **down** or
**recovers**.

```bash
api-qovrix serve                 # run until stopped (Ctrl+C, or SIGTERM from Docker/systemd)
api-qovrix serve --once          # check everything once through the same pipeline, then exit
api-qovrix services              # current status per service
api-qovrix history get-order     # recent runs of one monitor
```

It uses the same `api-qovrix.yaml` and monitor files as `run`. It runs next to your app, never inside it, so
the app's language and hosting don't matter.

## Settings

```yaml
defaults:
  interval: 1m          # how often each monitor runs (per monitor/service too)
  alert_after: 2        # failures in a row before a monitor is "down"
  alert_repeat: 1h      # optional: remind every hour while still down
  notify: [team-chat]   # see alerts.md

server:
  retention: 30d        # how long run history is kept
  group_wait: 10s       # group changes of the same service into one alert
  concurrency: 10       # monitors running at the same time
```

`alert_after` and `alert_repeat` can also be set per service or per monitor. `cron` schedules aren't supported
yet: use `interval`.

## How alerts work

- A monitor is **down** after `alert_after` failures in a row. That sends one alert and opens an incident.
- While it stays down, nothing more is sent, unless `alert_repeat` is set.
- The first success after that sends a **recovered** alert with how long it was down, and closes the incident.
- Changes in the same service within `group_wait` become **one** message per notifier, e.g.
  "orders: 2 monitors down, 1 recovered".
- State survives restarts, so a monitor that was down before a restart still sends its recovery message.

```text
🔴 api-qovrix: 2 monitors down in orders (shop-api · production)

• get-order fail: drift (breaking)
    $.total  integer → string (breaking)
• list-orders error: connection failed: ...
```

## Where history is stored

In a SQLite file: `.api-qovrix/data/api-qovrix.db` next to `api-qovrix.yaml` by default (that folder ignores
itself in Git). Change it with `--data-dir` or `API_QOVRIX_DATA`. History older than `retention` is deleted
automatically. Response bodies are never stored, and secrets are masked.

Drift detection in `serve` compares responses with the baselines committed in `.api-qovrix/baselines/`.

## Running it

| Where | How |
|---|---|
| Docker / Docker Compose | See [`examples/docker-compose`](../examples/docker-compose/docker-compose.yml): one container, your `monitoring/` folder mounted read-only, a volume for `/data`, and `API_QOVRIX_PASSWORD` for the dashboard |
| A VPS or VM | Install the binary and run `api-qovrix serve` as a system service (e.g. systemd), from the folder with `api-qovrix.yaml` |
| Kubernetes | One replica, a ConfigMap or volume with the monitor files, and a persistent volume for `/data` |

Run **one** `serve` process per project (it owns its SQLite file). Logs go to stdout, one line per check:

```text
2026-11-05T10:00:01Z  ✔ list-orders [orders] 200 84ms
2026-11-05T10:00:02Z  ✘ get-order [orders] 200 91ms → drift (breaking)
2026-11-05T10:00:12Z  alert sent → team-chat: orders: 1 monitor down · shop-api · production
```

Use `--log-format json` for log collectors.

To pick up changes to monitor files, restart the process.

The same process serves the dashboard and REST API at `http://localhost:8080`: see
[dashboard.md](dashboard.md).
