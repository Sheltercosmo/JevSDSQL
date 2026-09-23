# Architecture

JevSDSQL has three main responsibilities: interpret the request, establish the evidence needed by that interpretation, and execute authorized data operations. Keeping these responsibilities separate makes plans inspectable and allows independent semantic work to run in parallel.

## Components

| Component | Location | Responsibility |
| --- | --- | --- |
| HTTP service | `sdd/api.py`, `sdd/generic/api.py` | Authentication, tenant context and public query routes. |
| Catalog | `sdd/generic/catalog.py` | Dataset schemas, field descriptions, keys and relationships. |
| Query planning | `sdd/generic/planner.py` and adjacent planner modules | JEV planning, typed stages, hybrid proposals and review. |
| SQL execution | `sdd/generic/sql.py` | SQL validation, semantic predicates, result coverage and mutation previews. |
| Semantic features | `sdd/generic/features.py` | Definition revisions, evidence reuse, human corrections and materialization. |
| Operator API | `sdd/operators/service.py` | Operator contracts, authorization and dispatch. |
| Operator runtime | `sdd/operators/runtime.py`, `budget.py`, `types.py` | Batching, concurrency, reservations and explicit output states. |
| Workspace | `sdd/web/` | Separate English and Simplified Chinese interfaces. |

## Hybrid stage placement

| Stage | What must already be known | Work that can overlap | Reason for its position |
| --- | --- | --- | --- |
| Definition retrieval | The objective and authorized catalog | Independent rule pages | Relevant definitions guide later field selection. |
| Field retrieval | Applicable definitions and their dependencies | Independent schema pages | Context is reduced before the LLM sees it; keys and relationship bridges survive. |
| Value observation | Retained tables and fields | Bounded source reads | Actual values help interpretation without becoming an execution filter. |
| SQL generation | The retained context and objective | One bounded generation | The LLM proposes executable alternatives and states its assumptions. |
| Validation and review | Candidate SQL and implementation facts | Coverage, output, population, relationship, formula and operation checks | Each check receives the same candidate context and does not wait for unrelated judgments. |
| Local alternatives | A supported, identified defect | Checks for distinct alternatives | Deterministic changes can avoid another generation while retaining the original candidate. |
| Optional repair | A concrete compilation or semantic defect | Independent replacement checks | At most one additional generation is permitted; uncertainty alone does not trigger it. |
| Execution | A legal plan and any required user confirmation | Independent semantic row decisions within limits | SQL performs arithmetic and relational operations after the necessary decisions are available. |

Shared stages form a DAG. A dependency is a reason to wait; the visual arrangement of a plan is not. Compatible questions share context in a request, while independent contexts use bounded concurrency.

## Evidence and state

Evidence identity includes tenant, source revision, model, context and question. Compatible observations can be reused when a policy threshold changes. A changed source or meaning requires new evidence. Human assertions remain attributable and do not overwrite the model's original observation.

`VALUE`, `UNKNOWN` and `NOT_EVALUATED` describe result availability. Operational states describe what happened while obtaining that result. A conditional branch that was not executed cannot supply a false value to a dependent stage.

## Data boundaries

Clients authenticate with a server-controlled token mapped to a tenant, actor and role. SQL binds registered catalog objects and applies the same access rules to direct and generated queries. PostgreSQL row-level security provides a second tenant boundary. The service itself is trusted to set the authenticated tenant context.

Ordinary reads use a database transaction. Semantic reads retain the source evidence needed to interpret their results. Mutation previews check authorization, source freshness and confirmation before committing. Unresolved semantic membership prevents writes.

## Extending the system

Add a reusable operator or planning mechanism through its typed inputs and result contract. Record when a new stage depends on another result and what work can remain independent. Keep dataset names and expected benchmark answers out of production decisions. See [contribution guidance](../CONTRIBUTING.md) for tests and documentation.
