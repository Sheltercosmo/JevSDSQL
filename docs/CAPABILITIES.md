# Capabilities and operator selection

JevSDSQL separates language interpretation from data execution. JEV supplies bounded semantic decisions, while SQL and application code handle arithmetic, authorization, storage and workflow state. Use the table below to find the relevant API before consulting its full signature.

## Query and data workflows

| Objective | Interface | Result |
| --- | --- | --- |
| Ask a question about registered datasets | `POST /ask` | SQL proposal, planning decisions and optional query execution. |
| Inspect or correct an interpretation | `POST /ask/review` | Revised plan with the user's selected decisions. |
| Revisit an earlier request | `/query-history` | Saved requests and linked revisions. Opening history does not execute a query. |
| Execute SQL | `POST /data/sql` | A read result or a mutation preview, with a completeness manifest. |
| Create records from a document | `POST /data/extractions` | Typed entries with exact source evidence, followed by optional import. |
| Save a reusable semantic definition | `/features` | Reviewed Boolean, category, score or extraction features. |

The [workspace guide](USER_GUIDE.md) covers these flows without requiring SQL knowledge. The [text import guide](TEXT_IMPORT.md) explains the row and column descriptions used by extraction.

## Operator families

Call operators through `POST /jev/call`. A population may be supplied inline or identified by a registered dataset scope. The [function reference](JEV_FUNCTION_REFERENCE.md) documents exact arguments and examples.

| Task | Operators | When to use them |
| --- | --- | --- |
| Make a typed decision | `PROMPT`, `NOUL`, `CHOICE`, `SCORE` | Test a proposition, select a named alternative or apply an ordered rubric. |
| Organize a population | `CLASSIFY`, `TAG`, `FILTER`, `CLASSIFY_HIERARCHY` | Assign categories, evaluate independent labels or retain matching records. |
| Compare and prioritize | `COMPARE`, `RANK`, `RERANK`, `COMPOSITE_SCORE` | Compare pairs, score a population or combine explicit criteria. |
| Extract source material | `EXTRACT`, `EXTRACT_DATE`, `FIND`, `STRUCTURE`, `SUMMARY_EXTRACTIVE`, `EXTRACT_TABLE` | Select evidence, normalize supported dates, segment documents or build typed rows. |
| Connect records | `JOIN`, `ALIGN`, `RELATE`, `MATCH`, `EVIDENCE_JOIN` | Evaluate semantic relationships, identity candidates and bounded event patterns. |
| Check claims and requirements | `VERIFY`, `COVER` | Distinguish support, opposition, conflict and insufficient evidence. |
| Calculate over decisions | `AGGREGATE`, `CONTRAST` | Compute supported statistics and compare declared populations. |
| Control execution | `ROUTE`, `RESOLVE`, `TRACE`, `STATE_SCAN`, `WORKFLOW` | Select a handler, stop once an answer is established, compose states or run conditional stages. |
| Manage reusable meaning | `DISCOVER`, `REVIEW`, `PROMOTE`, `MATERIALIZE`, `REFRESH` | Propose definitions, record corrections and maintain approved generations. |
| Build or inspect semantic work | `EVALUATE`, `ENSURE_SEMANTICS`, `SELECT_SCHEMA`, `PLAN_SQL`, `EXPLAIN_PLAN` | Submit explicit work, reuse evidence, select schema or inspect a proposed query and its cost. |

## Choosing the right scope

`FILTER` evaluates its supplied population. `RERANK` only orders the shortlist you give it, so its highest-ranked item is not necessarily the best item in a larger corpus. `RESOLVE` can stop early once a declared condition is established; untouched subjects remain `NOT_EVALUATED`.

`EXTRACT` and `SUMMARY_EXTRACTIVE` return source passages. `EXTRACT_TABLE` maps exact spans into typed fields. They should be used when traceability to the source matters. Missing facts remain unresolved rather than being filled with generated text.

`JOIN`, `ALIGN` and `RELATE` return evidence about relationships. They do not merge stored identities. `ROUTE` selects from an allowlist without executing the chosen handler. `PLAN_SQL` returns a query proposal without executing it.

## What self-development means

`DISCOVER` creates provisional concepts from source examples. A reviewer checks independent examples and uses `PROMOTE` to approve a revision. `MATERIALIZE` publishes a resolved generation for that revision, and `REFRESH` rebuilds affected values when dependencies change. Human corrections are recorded separately from model observations.

This lifecycle supports growing a reusable semantic layer while keeping the source, definition, model evidence and human decision attributable. Approval is required for promotion; the system does not silently rewrite its own schema or treat discovered concepts as established facts.

## Limits that affect use

Direct operator populations currently support up to 5,000 registered rows and require primary keys. Operators have additional bounds for branching, matching and evidence bundles; see the [operator guide](JEV_OPERATORS.md#boundaries). Cost and time estimates are planning aids, not guarantees.

Semantic decisions can be uncertain or fail operationally. Check `output_state`, `operation_state` and coverage before using a value in another operation. Unknown membership can block a write or prevent a result from being described as complete.

JEV is an external service. The open-source project supplies the database integration, operators, execution controls and user interface; it does not bundle JEV model weights. Hybrid mode adds a separately configured LLM provider.
