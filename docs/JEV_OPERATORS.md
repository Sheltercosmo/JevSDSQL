# JEV operator guide

Use `POST /jev/call` to run the 41 database operators or a conditional WORKFLOW. Noul, Choice and Score are the provider primitives; the other operators compose them with database operations. These are API interfaces, not SQL function names.

[Function reference](JEV_FUNCTION_REFERENCE.md) · [Runnable examples](../examples/operators/README.md)

## First call

1. Follow the repository's [installation instructions](INSTALLATION.md) and start the server.
2. Use a database API token configured in `SDD_API_TOKENS`. The server uses `TYPESAFE_API_KEY` separately for JEV calls.
3. Open [the API reference](http://127.0.0.1:8000/docs), choose Authorize, and enter the database token. Under JEV operators, open `POST /jev/call`, choose an example and select Try it out.

A minimal request:

```json
{
  "operator": "JEV.NOUL",
  "arguments": {
    "state": "The work is complete.",
    "proposition": "The source reports completed work."
  },
  "limits": {"max_judgments": 1, "max_requests": 1}
}
```

The [Python examples](../examples/operators/README.md) show the equivalent HTTP call. `GET /jev/operators` provides signatures and usage; `GET /jev/operators/examples` provides complete request bodies. The [function reference](JEV_FUNCTION_REFERENCE.md) explains arguments, outputs and prerequisites individually.

## Read the result

| Field | Meaning |
|---|---|
| `output_state` | `VALUE`: resolved, including false/zero; `UNKNOWN`: unresolved; `NOT_EVALUATED`: no semantic result |
| `operation_state` | Execution status, such as `SUCCEEDED`, `SKIPPED`, `FAILED`, `BLOCKED_BY_BUDGET`, `TRUNCATED`, `CANCELLED` or `STALE` |
| `value` / `partial_value` | Complete answer / retained incomplete result |
| `observations` | Individual decisions, raw answers, revision IDs and cache status |
| `manifest` | Coverage, answer completeness, model/policy revisions, reservations and usage |
| `run_id` | Read the saved run with `GET /jev/runs/{run_id}` |

Never infer a Boolean from a missing value. A successful inference may return UNKNOWN; a skipped stage returns NOT_EVALUATED / SKIPPED. Required dependents of unavailable values are blocked. A valid existence answer can be complete while untouched subjects remain unexecuted. See the [workflow example](../examples/operators/workflow.py).

## Inputs and execution controls

Population parameters accept inline values or `{"dataset_id":"...","where":{"team":"North"}}`. Registered datasets need a primary key and currently support at most 5,000 rows. Definitions accept instruction strings, structured objects, or explicit `{"revision_id":"..."}` references. Direct states for NOUL, CHOICE, SCORE and COMPARE are literal data.

Place `limits`, `policy` and optional `approval_id` beside `operator` and `arguments`. The function reference shows operator-specific options; shared settings apply to the whole run.

| Default limit | Value |
|---|---|
| Judgments / requests | 1,000 / 1,000 |
| Input estimate / cost cap | 2 million tokens / $2 |
| Concurrent requests / shared-context questions | 4 / 32 |
| Retries / dynamic stage cap | 1 / 3 |

Compatible questions share context; independent contexts run concurrently. Workflow frontiers and speculative STATE_SCAN hypotheses preserve parallel execution. Raw evidence is cached by tenant, model, subject revision, context, question and hypothesis. Reweighting or changing thresholds can reuse compatible evidence; changed rows are checked before publication.

EXPLAIN_PLAN estimates work before execution. Large plans may require a reviewer to call `POST /jev/approve`; the returned approval covers the exact request, limits and source population. Hard and provider limits still apply. Token estimates use a conservative UTF-8 byte bound, and time/cost estimates are not guarantees. Failed attempts consume reservations.

`POST /jev/runs/{id}/cancel` prevents further dispatch; in-flight HTTP calls may finish. `POST /jev/runs/{id}/resume` creates a single child run using remaining reservations and cached observations. Changed sources require a new request.

## Review and maintain a concept

1. DISCOVER proposes a provisional concept. Retain its `candidate_revision`.
2. Review separate holdout examples, then call PROMOTE with the candidate, owner, actual validation report and retention policy. Retain `approved_revision.id`.
3. Call MATERIALIZE with that revision and a registered dataset scope. Only resolved, fresh generations publish; incomplete generations remain PARTIAL.
4. Call REFRESH with a generation ID when its dependencies change, or choose a bounded subscription.

REVIEW stores a human assertion against an observation; raw evidence remains immutable. REVIEW, PROMOTE, MATERIALIZE and REFRESH require a reviewer token. Retention policy text is approval metadata; it does not schedule deletion.

A refresh policy is either `{"mode":"explicit"}` or, for example, `{"mode":"on_change","max_refreshes":5,"interval_seconds":60}`. Subscriptions use the existing API maintenance worker and original execution envelope. They decrement their refresh allowance and require new approval if the model changes. `SDD_FEATURE_MAINTENANCE=0` disables the worker. Generations are stored snapshots; automatic SQL view creation is not included.

## Boundaries

- RANK is pointwise and preserves ties; RERANK covers its supplied shortlist. Pruned or unresolved work cannot establish exhaustive absence.
- EVIDENCE_JOIN supports eight sources and singleton/pair bundles. MATCH has six nodes, 20 neighbors, 1,000 verified candidates and a 10,000-assignment enumeration cap.
- RELATE retains whole-source evidence offsets rather than minimal quotations. TRACE needs uniquely ordered timestamps with offsets. STATE_SCAN relies on the caller's declared sufficient state representation.
- DISCOVER proposes exemplar-based concepts. Date parsing supports bounded ISO, English and Simplified Chinese forms. CONTRAST reports descriptive comparisons. PLAN_SQL returns a proposal, including held proposals; it does not execute SQL.
- Default thresholds are uncalibrated application policy. Throttling and duplicate-batch locks are process-local; distributed deployments need shared account coordination.

Use the [capability guide](CAPABILITIES.md#operator-families) to choose an operator and the [function reference](JEV_FUNCTION_REFERENCE.md) for its implemented contract.
