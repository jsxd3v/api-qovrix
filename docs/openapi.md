# OpenAPI

## Validate responses

```yaml
# api-qovrix.yaml
openapi:
  source: ./openapi.yaml       # or a URL
  validate_responses: true     # default
```

Every response is matched to an operation by method and path (server base paths like `/v1` are handled)
and validated against the documented schema for its status code (`200`, then `2XX`, then `default`). OpenAPI
3.0 (`nullable`, boolean `exclusiveMinimum`) and 3.1 are supported; local `$ref`s are resolved. Swagger 2.0
is not supported: convert it to OpenAPI 3 first.

- A status code the operation doesn't document fails the check.
- Requests that don't match any operation are not validated.
- Turn validation off for one monitor with `openapi: false`.

## Generate monitors

```bash
api-qovrix import openapi ./openapi.yaml            # or a URL
api-qovrix import openapi ./openapi.yaml --service shop --base-url https://staging.example.com
```

This writes `monitors/<service>.yaml` with one monitor per **GET/HEAD** operation, then enables validation
in `api-qovrix.yaml`. It never generates `POST`, `PUT`, `PATCH` or `DELETE` monitors: write those yourself,
with cleanup steps.

- Path and required query parameters use the spec's `example`, `default` or first `enum` value. When none
  exists, the monitor is written with `enabled: false` and a `CHANGE_ME` value to fill in.
- Auth from `securitySchemes` (bearer, basic, API key, OAuth2 client credentials) is added to the service
  with secret placeholders, which are declared in `api-qovrix.yaml`.
- Deprecated operations are skipped.

Run the import again after the spec changes: without `--force` it only lists added and removed operations
and leaves your edited file alone.
