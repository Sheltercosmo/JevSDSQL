# Reusable semantic features

A feature gives a text interpretation a reviewed name, type and revision. It becomes a virtual column in its dataset; the original source stays intact. Use the Semantic features panel at `/ask/en` or `/ask/zh`, or the authenticated API below.

## Define, test and review

Create a candidate with `POST /features`:

```json
{
  "dataset_id": "YOUR_DATASET_ID",
  "name": "needs_action",
  "column": "body",
  "definition": "The author explicitly requests an action. Exclude quoted requests attributed to another person.",
  "kind": "noul",
  "context_columns": [],
  "aliases": ["action_requested"],
  "maintain": true
}
```

`context_columns: []` uses only the selected source field. Add fields when meaning depends on them; `null` uses the full row. Definitions, types and dependencies are immutable: save a new revision to change them. A name or alias cannot shadow a physical column or another active feature.

| Kind | Definition contract | SQL result |
|---|---|---|
| `noul` | A Boolean meaning; independent features can overlap | TRUE, FALSE or NULL |
| `choice` | `criteria` maps category names to mutually exclusive descriptions | Category text or NULL |
| `score` | `criteria` contains 2 to 10 ordered level descriptions | Fractional score from 0 to levels−1, or NULL |
| `extract` | Select a passage or quoted span from the source | Original text or NULL; evidence includes offsets |

Score requires sufficient stated evidence as well as a confident rubric result. Choice and extraction require the selected probability to meet `confidence` (default 0.55). Noul uses the query's acceptance/rejection thresholds (defaults 0.8/0.2). These thresholds are operating rules, not calibrated accuracy guarantees.

1. `POST /features/{id}/preview` with `{"max_evaluations":100}` tests a candidate and returns values, coverage and evidence.
2. `POST /features/{id}/review` with `{"status":"active","reason":"Reviewed examples","examples":[{"text":"Please arrange a visit.","expected":true}]}` activates it. This records a reviewer decision; it does not automatically prove the definition's quality.
3. Saving and activating a new revision deprecates the previous active revision with that name. Previously retained evidence stays attributable to its revision.

Creation, activation, correction and refresh require the reviewer role. Readers can inspect definitions, query active features and preview candidates.

## Query and correct

```sql
SELECT id
FROM documents
WHERE SEMANTIC_FEATURE(body, 'needs_action');

SELECT SEMANTIC_FEATURE(body, 'reviewed_category') AS category, COUNT(*) AS total
FROM documents
GROUP BY SEMANTIC_FEATURE(body, 'reviewed_category');
```

SQL accepts an active feature's name, alias or ID. Natural language can use the same names: “Show records that need action” or “Show the average urgency_score.” The planner uses features for projection, filtering, grouping, arithmetic and sorting within its existing grammar.

`POST /features/{id}/assertions` records a correction:

```json
{"primary_key":{"id":1},"value":true,"reason":"Checked against the original source"}
```

Assertions preserve the authenticated reviewer and source dependencies. They override model evidence for that revision and source; changed dependencies invalidate them. Extraction corrections must be exact source substrings. A new correction does not overwrite the model's probabilities.

Features may select rows for UPDATE/DELETE. Unknown membership blocks the write. A preview must still be explicitly committed; source or semantic-review changes require a new preview.

## Parallelism and maintenance

Compatible features share one Jev request per record. Independent record requests overlap with `SDD_JEV_CONCURRENCY=4` by default (1 to 16); a process limit of 16 also applies. Database-backed daily request quotas span workers. Concurrency limits themselves are process-local, so multiple server processes need an appropriately divided limit.

`max_evaluations` counts attempted record-feature pairs, even when batched. A Score feature uses two provider questions. `SDD_DAILY_EVALUATIONS` retains its historical name but counts provider requests, including planning and retries. Reports separate requests, questions, tokens, unknowns, reuse and reviews.

With `maintain: true`, activation and committed writes enqueue a dataset refresh. The API worker processes configured tenants unless `SDD_FEATURE_MAINTENANCE=0`. A separate process can run:

```powershell
.\.venv\Scripts\python.exe -m sdd.cli feature-worker --tenant demo --once
```

Use `POST /features/refresh` with `{"dataset_id":"...","max_evaluations":1000}` for an explicit pass. Inspect `/feature-jobs`; cancel pending/running work through `POST /feature-jobs/{id}/cancel`. Complete runs publish a materialization reference. Partial or cancelled runs can retain evidence for reuse but cannot publish a complete generation.

Each maintenance attempt allows 1,000 new evaluations and at most three attempts per job. Larger or persistently ambiguous populations need further explicit refresh/review. External database writes do not enqueue jobs automatically. Extraction candidates are bounded original sentences and quoted spans, not arbitrary entity generation; features currently operate on one row and its declared columns.

See [architecture](ARCHITECTURE.md) for evidence dependencies and [performance and cost](PERFORMANCE_AND_COST.md) for release measurements.
