# Full-review spending controls

Prepared and tested offline on September 13, 2026. These changes do not enable a
new live budget, change provider credentials, switch models, or submit inference
requests. The separate W&B quick-screen ledger retains its existing $2 / 30
request scope.

## Verified pricing and the activation limit

| Model | Standard input / 1M | Output / 1M | Full-context reservation with 2,048 output tokens |
| --- | ---: | ---: | ---: |
| GPT-5.6 Terra | $2 | $12 | $5.286864 |
| GPT-5.6 Sol | $4 | $20 | $10.561440 |

Sources, opened September 13, 2026:
[Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
[Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), and
[pricing](https://developers.openai.com/api/docs/pricing).

Both models document a 1,050,000-token context. Inputs above 272,000 tokens use
twice the input rate and 1.5 times the output rate. Cache writes cost 1.25 times
the uncached input rate. Reservations cover the entire context at that higher
cache-write tariff; returned usage is also estimated conservatively without
assuming cache discounts. A global $2 budget therefore rejects these full-review
requests before queueing them. **Do not raise the budget to make them pass.**

The application explicitly requests the default service tier and one completion.
It caps visible plus reasoning output using `max_completion_tokens`, as described
in the [Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
An unexpected returned service tier leaves the reservation unknown and halts
the budget. Regional endpoints and unlisted models are not permitted.

The [vision guide](https://developers.openai.com/api/docs/guides/images-vision)
documents model-specific patch costs, but that alone does not prove a complete
Chat Completions input bound. The server transforms messages and JSON schemas;
we have not established an authoritative upper bound for that overhead. The
[input-token counting API](https://developers.openai.com/api/docs/guides/token-counting)
uses Responses, while the frozen evaluator uses Chat Completions. This change
does not assume those two protocols have identical tokenization or switch the
evaluation protocol. Character estimates or arbitrary overhead constants are
not used to grant additional budget.

## Prepared configuration

This example is not applied to `.env`. It is a fail-closed configuration: with
the current Terra/Sol evaluator, its $2 cap admits no inference requests.

```dotenv
DL_SPEND_LIMIT_USD=2
DL_SPEND_BUDGET_ID=full-review-app-20260913-v1
DL_SPEND_LEDGER_PATH=data/full-review-spend.sqlite3
DL_SPEND_PER_RUN_LIMIT_USD=0.5
DL_SPEND_MAX_PHYSICAL_ATTEMPTS=30
DL_SPEND_MAX_RUN_ATTEMPTS=10
DL_MAX_OUTPUT_TOKENS=2048
```

Omitting `DL_SPEND_LIMIT_USD` preserves existing unguarded full-review behavior;
health explicitly reports that the guard is disabled. Configuring it enables a
shared durable budget for all selected final providers, with no provider
fallback. An omitted per-run dollar limit equals the shared dollar limit.
Configured limits, attempt ceilings, and run policy are immutable for an
existing budget ID. The application never replaces an ID or resets reservations
to recover from exhaustion. Changing configuration is a separate operator action.

## Enforcement and scope

- Each physical HTTP attempt reserves money before dispatch. SDK retries are
  disabled. Guarded API job providers disable compatibility retries as well.
- One SQLite `BEGIN IMMEDIATE` transaction checks both the job and shared cap,
  then records the same reservation under both scopes. Two providers, threads,
  or processes cannot spend the same available balance simultaneously.
- API jobs bind immutable generated job IDs to cloned provider adapters. Nested
  worker threads retain that binding; unrelated jobs do not share mutable scope.
- Errors, timeouts, missing usage, and crash reservations remain held. They are
  not recorded as free requests. Valid usage releases only the unused amount.
- Successful usage beyond a reserved bound is retained as a violation and halts
  later requests. Already-running requests keep their reservations.
- API admission rejects unavailable, stale, or unaffordable pricing before a
  new job is created. Existing idempotent job references remain readable when
  the budget is exhausted. `/api/health.full_review_spending` exposes the reason,
  pricing evidence, per-run policy, shared usage, and minimum reservations.
- Price cards expire October 13, 2026 for these OpenAI models. Expiry blocks
  new requests until the price evidence is explicitly reverified.

This controls token-cost exposure for requests sent through this application
ledger and budget ID. It is not an OpenAI invoice cap and does not cover other
programs, accounts, taxes, non-token services, or requests sent outside the guard.
Ordinary CLI code that enables a run policy must bind `bundle.for_run(stable_id)`
before inference; otherwise it fails closed instead of bypassing the run limit.

## Offline validation

`tests/unit/test_full_review_spend.py` covers thread/process races, shared
probe/planner budgets, separate job limits, immutable policies, retained failed
reservations, service-tier rejection, zero retries, API scope binding,
idempotency after exhaustion, and real-price $2 admission refusal. Provider
responses in these tests are explicitly mocked; no inference is performed.
