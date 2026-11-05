# CLI reference

Every command accepts `--help`. Commands that load a project accept `--config/-c` (path to
`api-qovrix.yaml`; by default it is searched in the current and parent folders), `--env/-e` and
`--env-file`.

| Command | What it does |
|---|---|
| `api-qovrix init [DIR]` | Create `api-qovrix.yaml` and an example monitor |
| `api-qovrix validate [--strict]` | Check config, monitor files, secrets and the OpenAPI spec |
| `api-qovrix run [MONITOR...]` | Run monitors once (`--service`, `--tag`, `--junit`, `--json-out`, `--update-baselines`, `--notify`, `--html`, `--concurrency`, `-v`) |
| `api-qovrix serve [--host H] [--port P] [--no-web] [--once] [--data-dir DIR] [--log-format text\|json]` | Run monitors continuously, keep history, send down/recovered alerts, serve the dashboard and REST API |
| `api-qovrix services` | Current status per service (from `serve` history) |
| `api-qovrix history MONITOR [--limit N]` | Recent runs of one monitor |
| `api-qovrix notifiers list` | Show notifiers and which monitors use them |
| `api-qovrix notifiers test [NAME...]` | Send a test alert |
| `api-qovrix drift show MONITOR [--live]` | Show the learned shape (and current drift) |
| `api-qovrix drift learn [MONITOR...]` | Sample GET monitors several times (`--samples`, `--delay`) |
| `api-qovrix drift accept MONITOR...` | Replace baselines with the current response shape |
| `api-qovrix import openapi SOURCE` | Generate monitors from an OpenAPI file or URL |
| `api-qovrix --version` | Show the version |

Environment variables: `API_QOVRIX_ENV` selects the environment when `--env` is not given;
`API_QOVRIX_RUN_URL` sets the run link in alerts (GitHub Actions is detected automatically);
`API_QOVRIX_DATA` sets where `serve` stores history; `API_QOVRIX_HOST`, `API_QOVRIX_PORT`,
`API_QOVRIX_PASSWORD` and `API_QOVRIX_API_KEY` configure the dashboard and REST API.
