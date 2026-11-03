# Getting started

## Install

Pick one. Only the last option needs Python.

```bash
# macOS / Linux standalone binary (checks the SHA-256 checksum before installing)
curl -fsSL https://github.com/jsxd3v/api-qovrix/releases/latest/download/install.sh | sh

# Docker
docker run --rm -v "$PWD:/work" ghcr.io/jsxd3v/api-qovrix --version

# Python 3.12+
pip install api-qovrix
```

On Windows, download `api-qovrix-windows-x64.exe` from the
[releases page](https://github.com/jsxd3v/api-qovrix/releases).

## Create a project

A project is a folder with an `api-qovrix.yaml` file, usually inside the repo of the app you monitor:

```bash
cd my-app
api-qovrix init monitoring
cd monitoring
```

This creates:

```text
monitoring/
├── api-qovrix.yaml          # environments, secrets, defaults
├── monitors/example.yaml    # two example monitors against a public test API
└── .api-qovrix/             # learned response shapes (commit this folder)
```

## Run

```bash
api-qovrix run               # all monitors once
api-qovrix run example-todo  # one monitor
api-qovrix run -v            # every assertion and the timing breakdown
```

The exit code is `0` when everything passes and `1` when a monitor fails, so it works as a CI step.

## Record baselines

Drift detection compares responses with a learned shape. Record it once and commit it:

```bash
api-qovrix drift learn            # call each GET monitor 5 times to learn optional fields
git add .api-qovrix/baselines
```

From now on, `api-qovrix run` fails when a response changes in a breaking way. See [drift](drift.md).

## Monitor your own API

1. Declare the token in `api-qovrix.yaml` (the value itself stays in an environment variable):
   ```yaml
   secrets:
     API_TOKEN: ${env:API_TOKEN}
   ```
2. Add a file under `monitors/` that uses it (see [monitors](monitors.md)):
   ```yaml
   monitors:
     - name: my-endpoint
       request:
         url: https://api.example.com/v1/status
         auth: { type: bearer, token: "{{ secrets.API_TOKEN }}" }
       drift: { enabled: true }
   ```
3. Set the variable and record a baseline:
   ```bash
   export API_TOKEN=...
   api-qovrix run --update-baselines
   ```

If you have an OpenAPI spec, generate monitors from it: `api-qovrix import openapi ./openapi.yaml`.
