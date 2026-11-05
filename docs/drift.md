# Drift detection

Most API breakages still return `200 OK`: a field is renamed, a number becomes a string, a value becomes
`null`. Drift detection catches them by comparing each response with a **baseline**: the response shape
learned from earlier successful responses.

## Turn it on

```yaml
monitors:
  - name: get-order
    request: { url: /orders/1 }
    drift:
      enabled: true
      fail_on: breaking        # breaking (default) | warning | never
      min_samples: 5           # samples before a learned baseline is trusted
      ignore:                  # paths to skip (children are skipped too)
        - $.meta.request_id
        - $..updated_at        # any depth
        - $.items[*].etag
```

Or for every monitor: `defaults: { drift: { enabled: true } }` in `api-qovrix.yaml`.

## What is reported

| Change | Severity |
|---|---|
| A field that was always present is missing | breaking |
| A type changed (`string → integer`, `object → array`) | breaking |
| A value became `null` where it never was | breaking |
| The response is no longer JSON | breaking |
| `integer → number` (e.g. `10` became `10.5`) | warning |
| An array that was never empty is empty | warning |
| A string with few distinct values (an enum) has a new value | warning |
| A new field appeared | info |

To keep false alarms low:

- Fields seen in only some samples are **optional**; their absence is not drift.
- Array items are merged, so one list response already gives many samples.
- New fields are only `info`. APIs add fields all the time.
- An enum is only assumed after at least 10 observations with at most 8 distinct short values, so IDs and
  names never trigger "new value".
- A baseline with fewer than `min_samples` samples (and not accepted by you) only reports `info`.

## Baselines live in Git

Baselines are JSON files in `.api-qovrix/baselines/<monitor>.json`. Treat them like snapshot tests: create
them locally, commit them, and review changes in pull requests.

```bash
api-qovrix drift learn                     # call each GET monitor 5 times and merge the samples
api-qovrix run --update-baselines          # merge the current responses (accepts changes)
api-qovrix drift accept get-order          # replace the baseline with the current shape
api-qovrix drift show get-order            # print the learned shape
api-qovrix drift show get-order --live     # ...and compare with the API right now
```

In CI, a monitor without a baseline gives a **warning** (not a failure) with the command to create one. A
monitor with a baseline fails when a change reaches `fail_on`.

`--update-baselines` never records a step whose assertions failed.

## Privacy

Baselines are committed to repositories, so they **never contain raw response values**. They hold types,
how often each field was present, and, for short strings with few distinct values (like `status`), short
one-way fingerprints: the first 12 characters of a SHA-256 hash. That's enough to notice a new enum value
without storing the value. A fingerprint of a short, guessable value (`"paid"`) can be confirmed by someone
who guesses it, so don't rely on it to hide secrets. Fields that look sensitive
(passwords, tokens, keys, emails, phone numbers, names, addresses, cards...) don't even get hashes. Add more
field names with `redact:` in `api-qovrix.yaml`.

## Drift and OpenAPI

Both can run together. OpenAPI validation checks the response against what the spec promises; drift
detection checks it against what the API actually returned before, which also works for third-party APIs
without a spec.
