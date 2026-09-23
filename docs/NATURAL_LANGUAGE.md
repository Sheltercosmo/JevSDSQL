# Query API

Use `POST /ask` to turn a natural-language request into a SQL proposal. Requests authenticate with a database bearer token configured in `SDD_API_TOKENS`. The server derives tenant, actor and role from that token.

```json
{
  "question": "Show total quantity by supplier, largest total first.",
  "dataset_ids": ["deliveries"],
  "planner_mode": "jev",
  "execute": false,
  "max_evaluations": 100
}
```

`dataset_ids` accepts catalog IDs or logical names. Omit it to use the authenticated tenant's catalog. `execute: false` plans without running SQL or classifying source rows. Planning still incurs provider calls. `max_evaluations` caps new semantic row evaluations, while the tenant's daily request limit covers planning, execution and retries.

The workspace supports English and Simplified Chinese through separate pages. Column names and descriptions come from the catalog. Business definitions belong in catalog metadata or the request's `knowledge` field.

## Planning modes

| Mode | Behavior |
| --- | --- |
| `jev` | JEV selects typed operations, schema roles and values. Deterministic code compiles them into SQL. |
| `hybrid` | JEV selects context, an LLM proposes SQL, and JEV reviews the candidate. See [hybrid configuration](HYBRID_QUERY.md). |

JEV planning supports filters, joins, grouping, arithmetic, ranking, independent aggregate populations and bounded dependent stages. Independent decisions run concurrently in a shared stage graph. More complex compositions may produce a partial proposal or an unresolved plan. Hybrid can propose a wider range of SQL but can still misinterpret the request.

Potential read relationships inferred from field names, uniqueness and value overlap require review. They are not automatically promoted to declared foreign keys.

## Review and confirmation

A held plan returns HTTP 422 with `review_required`, `executed: false`, a `review_id` and structured decisions. Legal SQL remains visible. Each decision includes its selection, alternatives and model score.

`proposal.status` distinguishes a candidate, a partial read and an unresolved plan. `review.unresolved` identifies uncertainty or missing work. A partial read can omit unsupported operations or return a bounded source preview; it does not claim to answer the full request. An unresolved plan may have no executable SQL. There is no partial mutation fallback.

| Endpoint | Request | Effect |
| --- | --- | --- |
| `POST /ask/review` | `{"review_id":"...","corrections":{"decision-id":"option-id"}}` | Rebuild an unexecuted plan. Boolean decisions accept JSON true or false. |
| `POST /ask/confirm` | `{"review_id":"...","max_evaluations":100}` | Run a legal read or create a mutation preview. |
| `POST /data/sql` | SQL request as documented in OpenAPI | Run edited SQL through the same access and execution checks. |

Reviews bind to the request, tenant, actor and catalog definitions and expire after 15 minutes. Upstream corrections invalidate incompatible downstream choices. Explicit confirmation records the user's decision separately from model scores. Invalid SQL cannot be confirmed.

Choice probabilities below 0.55, Boolean probabilities between 0.2 and 0.8, and final checks below 0.8 require review. These are operating thresholds, not calibrated accuracy guarantees.

## Query history

| Endpoint | Purpose |
| --- | --- |
| `GET /query-history?limit=20&before=<last-id>` | Page through the current actor's saved requests. |
| `GET /query-history/{id}` | Read a saved question, SQL and result. |
| `POST /query-history/{id}/review` | Restore saved decisions in a new review session. |

Opening history does not execute SQL. Send `parent_history_id` with a revised `/ask`, `/data/sql` or `/ask/review` request to link it to the original. A restored review can reuse decisions if catalog definitions still match. Changed definitions require replanning.

Saved history does not restore a mutation commit token. Rerunning a write creates a fresh preview. History is scoped to tenant and actor; deleting a source redacts associated history content.

## Semantic queries and writes

Use `SEMANTIC(source_column, 'definition')` for a semantic predicate or `SEMANTIC_FEATURE(source_column, 'feature_name')` for an active [reviewed feature](SEMANTIC_FEATURES.md). The source must resolve to a registered base-table column.

Uncertain decisions retain `UNKNOWN`; unexecuted work retains `NOT_EVALUATED`. Neither becomes false. Check the result manifest for completeness before interpreting counts or absence.

Inserts, updates and deletes produce previews that must be committed separately. Unknown semantic membership blocks a write. Source or review changes invalidate a preview. Full-table updates and deletes require explicit `allow_all: true` on the SQL endpoint. Primary-key updates, joined or subquery mutations, DDL and arbitrary commands are rejected.

## Limits

| Area | Bound |
| --- | --- |
| Question | 4,000 characters; up to 20 selected datasets. |
| SQL | One allowed SELECT/set operation or plain INSERT/UPDATE/DELETE; 30,000 characters and 2,000 syntax-tree nodes. |
| Results and affected writes | At most 1,000 rows. |
| Imports | At most 10,000 rows and 64 columns per request. |
| Semantic work | At most 32 features per dataset/query, subject to evaluation and request budgets. |
| Source snapshots | Ordinary reads use a database transaction. Semantic queries and mutation previews are limited to 50,000 source rows. |

JEV planning has additional bounds: its parallel path considers up to 64 catalog fields and six output expressions; its staged path has primary/reference branches, bounded aggregate slots and ten output slots. Value lookup inspects at most 512 values per field. These constraints limit expressiveness even when the SQL executor could run a more complex handwritten query.

All SQL is restricted to authorized, registered tables and allowed functions. Arithmetic and storage use database types. Decimal imports use `NUMERIC(38,10)`; use text when that precision is unsuitable.

See the running service's `/docs` for complete request schemas and [performance and cost](PERFORMANCE_AND_COST.md) for release measurements.
