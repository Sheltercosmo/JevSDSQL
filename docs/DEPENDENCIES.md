# Dependencies

JevSDSQL uses these libraries for storage, API handling and SQL validation. Exact installed versions are recorded in `requirements.lock.txt`; supported ranges are in `pyproject.toml`.

| Component | Purpose | Upstream |
| --- | --- | --- |
| PostgreSQL | Persistent data, transactions and tenant policies | [postgresql.org](https://www.postgresql.org/) |
| SQLAlchemy | Database connections and SQL construction | [sqlalchemy.org](https://www.sqlalchemy.org/) |
| Psycopg | PostgreSQL driver | [psycopg.org](https://www.psycopg.org/) |
| SQLGlot | Parse, inspect and translate SQL syntax | [tobymao/sqlglot](https://github.com/tobymao/sqlglot) |
| FastAPI and Pydantic | HTTP routes, request validation and OpenAPI | [FastAPI](https://github.com/fastapi/fastapi), [Pydantic](https://github.com/pydantic/pydantic) |
| Uvicorn | ASGI server | [encode/uvicorn](https://github.com/encode/uvicorn) |
| HTTPX | Provider and client HTTP requests | [encode/httpx](https://github.com/encode/httpx) |
| pytest, Ruff and Playwright | Tests, static checks and browser verification | [pytest](https://github.com/pytest-dev/pytest), [Ruff](https://github.com/astral-sh/ruff), [Playwright](https://github.com/microsoft/playwright-python) |

Each dependency retains its upstream license. The project's Apache 2.0 license applies to original JevSDSQL code and documentation.

JEV is provided by the external TypeSafe service. Its API key is configured on the server; model weights are not distributed here. Hybrid mode uses a separately configured LLM through the supported API or local CLI transport. Service terms and charges are separate from this project's license.
