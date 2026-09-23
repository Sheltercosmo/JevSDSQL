# Query workspace guide

[简体中文](zh/USER_GUIDE.md) · [All documentation](README.md)

Start the service, open [/ask/en](http://127.0.0.1:8000/ask/en), and connect with your database API token. The server's JEV and LLM credentials are separate; do not paste them into the workspace token field. Select the datasets relevant to your question.

## Choose how to query

| Mode | Use it for | How it works |
| --- | --- | --- |
| Natural language · JEV | Requests supported by the typed planner | Parallel semantic decisions assemble a checked SQL proposal. No LLM generation call. |
| Hybrid · LLM + JEV | Broader language and more complex SQL | JEV narrows context; the LLM proposes SQL; JEV reviews independent questions in parallel. |
| SQL | A query you want to write or edit directly | The same data-access and execution checks apply. |

Hybrid needs [LLM configuration](HYBRID_QUERY.md#configuration). Its default uses one generation call; a supported repair may add one more.

Describe the outcome you want, including business definitions, units and time periods when they matter. For example: “For each customer, show the latest payment and the change from their previous payment.” Selecting fewer relevant datasets helps focus the request without changing their stored data.

Choose Preview plan to inspect the interpretation before execution. Alt+Enter is the shortcut. Ctrl+Enter runs a read or previews a database change. These shortcuts apply while editing the query.

## Inspect and correct a plan

Hybrid results show How this plan was built: related data, SQL proposal and parallel review. Expand usage details for LLM calls, JEV planning requests, retained fields and review waves. Sampled values help planning; they do not restrict execution to the sample.

Under Interpretations, compare assumptions and SQL. Use this interpretation rebuilds the preview using that choice; it does not execute it. Local refinements remain identifiable and the earlier alternatives stay available. The detailed data constraints describe the proposed SQL, not proof that it expresses your intention.

If the plan is held, inspect its review items. Correct a decision, choose another interpretation, edit SQL, or explicitly confirm the available proposal. Invalid SQL must be fixed before execution. A review passing its checks is not a guarantee of a correct answer.

| Result state | Meaning |
| --- | --- |
| `VALUE` | A result exists, including a valid false or zero. |
| `UNKNOWN` | The requested result could not be resolved. |
| `NOT_EVALUATED` | This work was not evaluated. It is not false. |

Execution status is separate. `FAILED`, `BLOCKED_BY_BUDGET` and `TRUNCATED` explain operational limits. A proposal, review result and execution result may therefore have different states. Partial results should not be interpreted as a complete population.

Database changes have a separate preview and Commit changes action. Reviewing a plan or selecting an alternative never commits a write.

## Reuse a recent query

Open Recent queries and select an entry. Restore its question or SQL, or choose Review saved decisions to correct its interpretation. Restoring never executes the query. Run the edited request to create a linked revision while preserving the original.

Unchanged hybrid decisions can reuse the saved proposal and review work. A changed request or context can require new calls; reuse is not a promise of zero cost for every edit.

## Create records from text

Under Manage data, open Extract entries from text:

1. Choose a destination and describe what one row represents.
2. Define each column's name, type and meaning. Include units and number conventions.
3. Paste the document or load a UTF-8 text file, then extract.
4. Review the proposed records and their source excerpts before importing.

The extractor maps exact text spans to typed values. Missing or ambiguous required values hold the import instead of inventing entries. Automatic import is optional and inserts only fully resolved records. Repeating extraction can create duplicates; it is not an update or deduplication operation. See the [text import guide](TEXT_IMPORT.md) for API calls, numeric formats and limits.

## Use operators directly

The API exposes a catalog at `GET /jev/operators`, examples at `GET /jev/operators/examples`, and execution at `POST /jev/call`. Start with the [operator guide](JEV_OPERATORS.md), then use the [function reference](JEV_FUNCTION_REFERENCE.md) and [runnable examples](../examples/operators/README.md).

The workspace's Guide contains a shorter version of these workflows. For measured capabilities and limitations, see the [performance and cost guide](PERFORMANCE_AND_COST.md).
