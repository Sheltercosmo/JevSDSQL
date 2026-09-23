# JevSDSQL

JevSDSQL is a PostgreSQL application for querying and modifying data in natural language. It combines SQL with JEV semantic judgments, so queries can use both stored values and the meaning of text. It includes a web workspace, an HTTP API and reusable semantic operators.

JEV is a model service from TypeSafe that evaluates propositions, chooses between supplied options and scores text against a rubric. JevSDSQL uses these decisions to select data, construct or review SQL, and retain evidence for later use. PostgreSQL handles joins, arithmetic and storage.

[Installation](docs/INSTALLATION.md) · [User guide](docs/USER_GUIDE.md) · [简体中文](docs/zh/USER_GUIDE.md) · [Operator reference](docs/JEV_FUNCTION_REFERENCE.md)

## Features

* Query data in English or Simplified Chinese. Inspect the SQL, correct its interpretation and rerun it from query history.
* Choose JEV planning or hybrid planning. In hybrid mode, JEV selects relevant context, an LLM proposes SQL, and JEV reviews the proposal.
* Filter and classify text by meaning. Save reviewed definitions as reusable features, such as whether a message requests action.
* Extract database entries from documents. Describe the rows and columns, then review typed values alongside their source text before importing.
* Preview inserts, updates and deletes before committing them.
* Call semantic operators for extraction, ranking, matching, verification and conditional workflows.

The self-developing part is the semantic layer: definitions, evidence and corrections can be saved, reviewed, reused and refreshed as data changes. New concepts require approval before promotion.

## Getting started

You need Python 3.11 or newer, PostgreSQL and a TypeSafe API key. Python 3.13 and PostgreSQL 17 are the verified configuration. Hybrid mode also requires a configured LLM provider.

```bash
git clone https://github.com/Sheltercosmo/JevSDSQL.git
cd JevSDSQL
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` in Bash, then install:

```bash
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
```

Copy `.env.example` to `.env` and follow the [database setup](docs/INSTALLATION.md#postgresql) to create the application role and configure credentials. Then run:

```bash
python -m scripts.migrate_generic
python -m sdd.cli serve
```

Open the [English workspace](http://127.0.0.1:8000/ask/en) or [Simplified Chinese workspace](http://127.0.0.1:8000/ask/zh). Connect with your database access token and import data through Manage data.

## Query your data

Select a dataset and enter a question such as:

> For each supplier, show the total quantity delivered, largest total first.

Choose Preview plan to inspect the SQL. You can edit a planning decision, select an alternative interpretation or edit the SQL directly. Run the query when the proposal matches your intent. Data changes require a separate commit.

The same workflow is available through `POST /ask`:

```json
{
  "question": "For each supplier, show the total quantity delivered, largest total first.",
  "dataset_ids": ["deliveries"],
  "planner_mode": "hybrid",
  "execute": false
}
```

Use an ID or name from your catalog in `dataset_ids`. Set `planner_mode` to `jev` to plan without LLM generation. See the [query API guide](docs/NATURAL_LANGUAGE.md) and the local [API reference](http://127.0.0.1:8000/docs) for authentication and complete requests.

## Semantic operators

The API provides 41 operators and `WORKFLOW`. For example, send this request to `POST /jev/call` to test a proposition:

```json
{
  "operator": "JEV.NOUL",
  "arguments": {
    "state": "The shipment arrived on Tuesday.",
    "proposition": "The shipment has arrived."
  },
  "limits": {"max_judgments": 1, "max_requests": 1}
}
```

Results distinguish a known value, an unknown answer and work that was not evaluated. Independent judgments can run in parallel; compatible evidence can be reused.

Use the [operator guide](docs/JEV_OPERATORS.md) to select an operator and set budgets. The [function reference](docs/JEV_FUNCTION_REFERENCE.md) includes arguments, examples and result contracts for every operator.

## Performance and limitations

For this release, a local comparison on 100 BIRD Challenging questions produced the following results. One reference timed out, leaving 99 scored questions. Held proposals were included.

| Method | Matching SQL answers | Median request time | Estimated cost per 100 attempts |
| --- | ---: | ---: | ---: |
| JEV | 20/99 | 8.55 s | $0.389 |
| LLM baseline | 39/99 | 8.68 s | $3.262 |
| Hybrid | 34/99 | 14.23 s | $2.839 |

Timing covers 94 questions run with up to three concurrent cases. Costs use recorded usage and fixed assumed rates; they are not current prices or subscription charges. See [performance and cost](docs/PERFORMANCE_AND_COST.md) for the measurement conditions and missing usage.

Hybrid cost less on this sample but did not outperform the LLM baseline in accuracy or speed. JEV planning supports a bounded set of query structures. All modes can produce incorrect interpretations; inspect proposals and result completeness before relying on them. JEV and LLM model weights are not included.

## Documentation and contributions

See the [documentation index](docs/README.md) for text imports, semantic features and architecture. To report a problem, include a small synthetic dataset, the request and the expected result. Development setup and checks are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache 2.0](LICENSE). Third-party software retains its own licenses. See [NOTICE](NOTICE) and [dependencies](docs/DEPENDENCIES.md).
