# Dashboard and REST API

`api-qovrix serve` also serves a dashboard and a REST API, in the same process as the scheduler.

```bash
API_QOVRIX_PASSWORD='a-long-password' api-qovrix serve      # http://localhost:8080
api-qovrix serve --port 9000                                  # another port
api-qovrix serve --no-web                                     # scheduler and alerts only
```

## Pages

- **Overview**: every service and monitor with status (up, down, failing 1/2, paused), last check, response
  time, 24-hour uptime and a mini response-time chart. Updates itself every 15 seconds.
- **Monitor**: response-time chart (failures marked), the last run's checks and drift diff, recent runs
  (click one to see its details), incidents, and a **Run now** button.
- **Incidents**: open and resolved incidents with how long they lasted.

Pages are rendered on the server, with no external scripts, fonts or CDNs, and work in light and dark mode
and on phones.

## Access and security

| Setting | Meaning |
|---|---|
| `--host` / `API_QOVRIX_HOST` | Address to listen on. Default `127.0.0.1` (this machine only). The Docker image uses `0.0.0.0`. |
| `--port` / `API_QOVRIX_PORT` | Port, default `8080`. |
| `API_QOVRIX_PASSWORD` | Dashboard login. **Required** (8+ characters) unless listening on `127.0.0.1` only. |
| `API_QOVRIX_API_KEY` | Key for the REST API (`Authorization: Bearer <key>`), for scripts and other tools. |

- Without a password the dashboard is open, which `serve` only allows on `127.0.0.1`. On any other address
  it refuses to start.
- Logins use a signed, `HttpOnly`, `SameSite=Lax` session cookie (marked `Secure` behind HTTPS) that lasts
  12 hours or until the process restarts. Wrong passwords are rate-limited per client.
- Every response has a strict Content-Security-Policy and anti-framing headers. Cross-site form posts are
  refused.
- For access over the internet, put it behind your existing reverse proxy with HTTPS (e.g. at
  `monitor.example.com`), or keep it private and reach it through a VPN or SSH tunnel.

## REST API

All endpoints need `Authorization: Bearer $API_QOVRIX_API_KEY` (or a logged-in browser session), except
`/healthz`.

| Method | Path | Returns |
|---|---|---|
| GET | `/healthz` | `{"status": "ok", "version": ...}`: for load balancers and container health checks |
| GET | `/api/v1/services` | Services with their monitors and status, plus counts |
| GET | `/api/v1/monitors?service=` | Monitors with status, last result and 24 h uptime |
| GET | `/api/v1/monitors/{name}` | One monitor and its last 20 runs |
| GET | `/api/v1/monitors/{name}/runs?limit=` | Run history (newest first) |
| GET | `/api/v1/runs/{id}` | Full run details: steps, checks, drift changes (never response bodies) |
| POST | `/api/v1/monitors/{name}/run` | Run a monitor now; returns its status |
| GET | `/api/v1/incidents?state=open` | Incidents (`open` or all) |
| GET | `/api/v1/openapi.json` | OpenAPI description of this API |

```bash
curl -H "Authorization: Bearer $API_QOVRIX_API_KEY" https://monitor.example.com/api/v1/services
```

Example: run a check right after a deploy, from any CI or script:

```bash
curl -X POST -H "Authorization: Bearer $API_QOVRIX_API_KEY" \
  https://monitor.example.com/api/v1/monitors/checkout-flow/run
```
