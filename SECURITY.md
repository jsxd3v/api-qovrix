# Security policy

## Supported versions

Security fixes are released for the latest minor version.

## Reporting a vulnerability

Please **do not** open a public issue. Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/jsxd3v/api-qovrix/security/advisories/new).

Include the version, how to reproduce it and the impact you see. You'll get an answer within a few days, and
credit in the release notes if you'd like.

## How api-qovrix handles sensitive data

- Secrets are read from environment variables only. `api-qovrix.yaml` refuses literal secret values.
- Secret values and OAuth2 tokens are replaced with `***` in terminal output and JSON/JUnit reports.
  Response bodies are never included in reports.
- Baselines in `.api-qovrix/baselines/` hold types, presence counts and short one-way fingerprints (truncated
  SHA-256) of enum-like string values, never the raw values. Sensitive-looking fields get no fingerprints.
- `{{ ... }}` expressions run in a sandboxed Jinja2 environment, so they cannot run code.
- `allowed_hosts` can restrict which hosts monitors may call.
- Release binaries come with SHA-256 checksums, and the install script verifies them.
