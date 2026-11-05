# Monitors

A monitor file holds a list of monitors (and optionally a `service:` block, see [configuration](configuration.md#services)).

## Single request

```yaml
monitors:
  - name: get-order            # unique across the project
    description: Order detail
    tags: [orders, smoke]      # run with --tag smoke
    enabled: true
    timeout: 5s
    retries: 2                 # retry the whole monitor when it fails or errors
    vars: { order_id: 1001 }
    request:
      method: GET              # default GET
      url: /v1/orders/{{ vars.order_id }}
      headers: { Accept: application/json }
      params: { expand: customer }       # query string
      # json: {...} | body: "raw text" | form: {...}
      follow_redirects: true
      verify_tls: true
    assert:
      - status: 200
    drift: { enabled: true }
    openapi: true              # validate against the project's OpenAPI spec (default when configured)
```

Without a `status` assertion, any response with status 400 or higher fails the monitor.

## Flows

Use `steps` to chain requests. Values from `extract` are available to later steps as
`{{ steps.<step-name>.<value> }}`. After a failure, the remaining steps are skipped, except those with
`always_run: true` (use them for cleanup).

```yaml
monitors:
  - name: checkout-flow
    steps:
      - name: login
        request: { method: POST, url: /auth/token, json: { client_id: "{{ secrets.CLIENT_ID }}" } }
        extract:
          token: $.access_token           # JSONPath into the response body
          request_id: header:X-Request-Id # or a response header
      - name: create-cart
        request:
          method: POST
          url: /carts
          headers: { Authorization: "Bearer {{ steps.login.token }}" }
        extract: { cart_id: $.id }
        assert: [{ status: 201 }]
      - name: cleanup
        always_run: true
        request: { method: DELETE, url: "/carts/{{ steps.create-cart.cart_id }}" }
```

Each step can also have its own `drift:` settings and a `description`.

If every monitor of a service needs the same login, don't repeat a login step in each one: use
`token_request` auth on the service (below), so they share one login.

## Auth

Set `auth` on a request, a monitor or a service.

```yaml
auth: { type: bearer, token: "{{ secrets.TOKEN }}" }
auth: { type: basic, username: api, password: "{{ secrets.PASSWORD }}" }
auth: { type: api_key, name: X-API-Key, value: "{{ secrets.KEY }}" }          # header (default)
auth: { type: api_key, name: api_key, in: query, value: "{{ secrets.KEY }}" } # query string
auth:
  type: oauth2_client_credentials
  token_url: https://auth.example.com/oauth/token
  client_id: "{{ secrets.CLIENT_ID }}"
  client_secret: "{{ secrets.CLIENT_SECRET }}"
  scope: read:orders          # optional
  audience: https://api.example.com  # optional
  client_auth: basic          # or post (credentials in the form body)
```

OAuth2 tokens are fetched once per run and refreshed shortly before they expire.

### Shared login (`token_request`)

For APIs where you sign in with a request that returns a token (email and password, a session endpoint, a
custom auth service), describe that login once. api-qovrix calls it the first time a monitor needs it,
reuses the token for every monitor with the same login, and logs in again shortly before it expires.

```yaml
# monitors/app.yaml
service:
  base_url: "{{ secrets.APP_URL }}"
  headers: { apikey: "{{ secrets.APP_PUBLIC_KEY }}" }   # sent on every request
  auth:
    type: token_request
    request:
      method: POST                                      # default
      url: "{{ secrets.APP_URL }}/auth/v1/token?grant_type=password"
      headers: { apikey: "{{ secrets.APP_PUBLIC_KEY }}" }
      json: { email: "{{ secrets.MONITOR_EMAIL }}", password: "{{ secrets.MONITOR_PASSWORD }}" }
    token: $.access_token        # where the token is in the response (default)
    expires_in: $.expires_in     # its lifetime in seconds (default); if missing, `lifetime` is used
    lifetime: 1h                 # default
    header: Authorization        # default
    prefix: "Bearer "            # default
monitors:
  - name: invoices
    request: { url: /rest/v1/invoices?limit=1 }
  - name: journal
    request: { url: /rest/v1/journal_entries?limit=1 }
```

Both monitors above share one login. With `serve`, the token is reused across checks until it expires. The
token and the login's secrets are masked in every report; a failed login shows only the status code.

`{{ ... }}` works in `base_url` too, so a service's address can come from a secret or a variable.

## Assertions

Each assertion has exactly one target.

| Target | Example |
|---|---|
| `status` | `status: 200`, `status: 2xx`, `status: [200, 204]` |
| `response_time_ms` | `response_time_ms: 800` (less than), or `{ lt: 800 }`, `{ gte: 10, lt: 500 }` |
| `jsonpath` | `jsonpath: $.status` + an operator |
| `header` | `header: content-type` + an operator (case-insensitive name) |
| `body` | `body: { contains: "ok" }`, `{ not_contains: "error" }`, `{ matches: "^\\{" }` |
| `json_schema` | inline schema, or a path to a `.json` schema file |

Operators for `jsonpath` and `header` (several can be combined; all must pass):

| Operator | Passes when |
|---|---|
| `equals` / `not_equals` | the value is (not) equal |
| `in` / `not_in` | the value is (not) one of a list |
| `contains` / `not_contains` | a string contains text, or a list/object contains an item/key |
| `matches` | the value matches a regular expression |
| `gt`, `gte`, `lt`, `lte` | numeric comparison |
| `length` | a string, list or object has this length |
| `type` | `string`, `integer`, `number`, `boolean`, `array`, `object`, `null` |
| `exists` | `true`: the path matches something; `false`: it doesn't |
| `not_empty` | the value is not `null`, `""`, `[]` or `{}` |

In YAML, quote a JSONPath that contains `*` when it sits inside `{ }` or `[ ]`, because `*` has a meaning of
its own in YAML: `{ jsonpath: "$[*].email", not_empty: true }`. On its own line no quotes are needed.

JSONPath uses the extended syntax of [jsonpath-ng](https://github.com/h2non/jsonpath-ng): `$.items[*].id`,
`$..price`, `$.items[?(@.stock < 1)]`. A path with a wildcard or filter returns a list:

```yaml
- jsonpath: $.items[*].status
  not_contains: failed
```
