# Contributing to JevSDSQL

Contributions are welcome in query planning, semantic operators, execution correctness, usability and evaluation. Open an issue describing the problem and the behavior you expect, or submit a focused pull request with a reproducible example.

## Development setup

Follow [installation](docs/INSTALLATION.md), then install the development extras in the same Python environment:

```bash
python -m pip install -e ".[test,dev]"
python -m pytest -q
```

The ordinary suite uses deterministic fixtures and does not call JEV or an LLM. Tests requiring PostgreSQL are skipped unless `SDD_TEST_POSTGRES_URL` is configured. With a local database configured in `.env`, `python -m scripts.run_tests` enables those checks.

Run `ruff check sdd scripts tests` for static checks. Keep formatting changes separate from behavior changes when possible. Browser tests need the `browser-test` extra and Edge. Run `python -m scripts.test_hybrid_ui` and `python -m scripts.test_text_import_ui` for isolated UI checks. `python -m scripts.test_ui_states` requires the service at `http://127.0.0.1:8000`.

## Planner and operator changes

A change should address a reusable mechanism, such as entity identity, join multiplicity, time boundaries, output units or missing evidence. Production code must not recognize a benchmark question, database name or expected answer.

Preserve independent work in a shared stage DAG. Explain a new dependency or synchronization point in [the architecture guide](docs/ARCHITECTURE.md). Keep `VALUE`, `UNKNOWN` and `NOT_EVALUATED` distinct, and retain execution failures as separate status information.

Use development cases to investigate and validation cases to choose an approach. Freeze the implementation before a final test. A previously inspected test becomes regression data when it influences a change. Check paraphrases, Simplified Chinese, schema renaming, duplicates, NULLs, empty populations and ties where relevant.

## Documentation

Explain the user-visible behavior first. Include a small working example, the result contract and the limits that affect use. Prefer short paragraphs and tables over repeated bullet lists. Use ordinary punctuation and avoid promotional claims.

Operator requests and usage descriptions live in `sdd/operators/examples.json`. Generate the reference after editing them:

```bash
python -m scripts.build_operator_docs
python -m scripts.build_operator_docs --check
```

Keep measurements attached to their protocol and version. Distinguish execution accuracy from unit-test success, and measured token usage from estimated dollar cost.

## Pull requests

Describe the concrete problem, the resulting behavior and the checks you ran. Include known limitations that affect a reviewer. Do not commit provider keys, database tokens, `.env`, runtime databases or downloaded datasets. Examples should use synthetic data or clearly attributed public material.

Original contributions are accepted under the project's [Apache 2.0 license](LICENSE). Preserve third-party notices and dataset licenses.
