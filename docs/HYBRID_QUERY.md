# Hybrid queries

Hybrid mode uses JEV to select relevant context, an LLM to propose SQL, and JEV to review the proposal. Choose Hybrid in the workspace or set `planner_mode` to `hybrid` in `POST /ask`.

```json
{
  "question": "For each customer, show their latest payment and the change from the previous payment.",
  "planner_mode": "hybrid",
  "dataset_ids": ["customers", "payments"],
  "execute": false
}
```

Use dataset IDs or names from your authenticated catalog. Omit `dataset_ids` to use the tenant's catalog. Supply business definitions through `knowledge`. Setting `execute` to false returns a plan without executing SQL.

## Configuration

Configure these server environment variables:

| Setting | Purpose |
| --- | --- |
| `TYPESAFE_API_KEY` | JEV provider credential. |
| `OPENAI_API_KEY` | LLM provider credential for the API transport. |
| `SDD_LLM_TRANSPORT=openai` | Use the Responses API. |
| `SDD_LLM_MODEL` | A structured-output model available to your provider account. |
| `SDD_HYBRID_CONCEPTS=off` | Skip a separate preliminary concept call; this is the default. |
| `SDD_HYBRID_REPAIR=on` | Allow one additional generation for a concrete defect; this is the default. |

For local use, `SDD_LLM_TRANSPORT=codex_cli` uses an installed, signed-in CLI with tools disabled. Select a model available to that CLI. `SDD_CODEX_EXECUTABLE` can specify its executable path. This transport includes process startup time.

Concept mode `auto` lets JEV request a short concept call for vague objectives or unfamiliar vocabulary; `on` always adds it. Concepts guide retrieval and do not become row filters. With concepts and repair both `off`, a request uses one LLM generation.

## How a request is processed

| Stage | Work |
| --- | --- |
| Retrieve context | JEV selects relevant definitions and fields, retaining keys and relationship bridges. Independent pages are checked in parallel. |
| Observe values | Read bounded samples once and retain them for generation and review. |
| Propose SQL | The LLM supplies complete SQL, assumptions and up to two alternatives. Code derives the operation graph and column usage from the SQL. |
| Validate and review | Tenant and SQL guards check legality. JEV reviews missing data, operations, outputs, populations, relationships, formulas and overall suitability in parallel. |
| Resolve defects | Supported local transformations create alternatives without another generation. A compilation error or corroborated semantic defect can trigger one LLM repair. |
| Execute | Run legal SQL after any required user confirmation. Writes produce a separate commit preview. |

Value observation samples at most 256 rows from each of 16 tables and 24 retained columns. Each column retains up to 16 example values, with text bounded to 160 characters. Samples help interpretation; execution still uses the full authorized source.

A semantic repair requires a defect-category probability of at least 0.8 and a corresponding negative check of at most 0.2. Uncertainty alone holds the proposal for review. Output-only repair cannot change joins, filters, shared CTEs or ordering. Failed replacements leave the original proposal available.

Arithmetic runs in SQL. A semantic predicate can use `SEMANTIC(source_column, 'definition')`, which invokes JEV through the evidence and budget machinery.

See [architecture](ARCHITECTURE.md#hybrid-stage-placement) for stage dependencies and the reasons for their placement.

## Inspect and correct a result

The workspace shows the retained data, proposed SQL and review checks. Select Use this interpretation to rebuild a preview from an alternative. It does not execute the query. Query history preserves earlier requests and supports correction of saved decisions.

`plan.hybrid` includes retrieval sizes, data constraints, candidates, checks, repair information and usage. These fields describe the proposed implementation and its review; they do not prove that it answers the user's question.

Results distinguish `VALUE`, `UNKNOWN` and `NOT_EVALUATED` from operational statuses such as `FAILED` or `BLOCKED_BY_BUDGET`. A failed review retains legal SQL for inspection and prevents automatic execution. Invalid SQL retains its validation error and must be fixed before it can run.

Unchanged review work can be reused within a request and from correction history. Cache identity includes tenant, model, input state and questions. Corrections are bound to the reviewed SQL and do not silently transfer to changed alternatives.

Measured accuracy, latency and usage for this release are in [performance and cost](PERFORMANCE_AND_COST.md).
