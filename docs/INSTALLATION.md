# Installation

JevSDSQL runs as a Python service backed by PostgreSQL. The verified environment uses Python 3.13 and PostgreSQL 17. SQLite is used by the deterministic test suite.

## Python environment

```bash
git clone https://github.com/Sheltercosmo/JevSDSQL.git
cd JevSDSQL
python -m venv .venv
```

In PowerShell, activate with `.venv\Scripts\Activate.ps1`. In Bash, use `source .venv/bin/activate`. Then install the verified dependencies and the local package:

```bash
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
```

Copy `.env.example` to `.env`. The examples below use placeholders; choose your own credentials. URL-encode special characters when putting a password in a database connection URL.

## PostgreSQL

You can use an existing PostgreSQL 17 installation or the included Docker Compose service. The service binds PostgreSQL to localhost and stores data in a named volume.

For Docker, add `POSTGRES_PASSWORD=<admin-password>` to `.env`, then run:

```bash
docker compose up -d
docker compose exec postgres psql -U sdd_admin -d sdd
```

At the `psql` prompt, create the restricted runtime role:

```sql
CREATE ROLE sdd_app LOGIN NOSUPERUSER NOBYPASSRLS;
\password sdd_app
GRANT CONNECT ON DATABASE sdd TO sdd_app;
GRANT USAGE ON SCHEMA public TO sdd_app;
\q
```

The password prompt avoids putting the application password in a command or SQL history. For an existing installation, connect as its administrator, create the `sdd` database if needed, and apply the same role setup. Run `CREATE ROLE` only when the role does not already exist.

## Server configuration

Set these values in `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg://sdd_app:<app-password>@127.0.0.1:5432/sdd
SDD_ADMIN_DATABASE_URL=postgresql+psycopg://sdd_admin:<admin-password>@127.0.0.1:5432/sdd
TYPESAFE_API_KEY=<your-jev-provider-key>
SDD_API_TOKENS={"<your-database-token>":{"tenant":"demo","name":"owner","role":"reviewer"}}
```

The database token authenticates a person or client to JevSDSQL. The JEV provider key authenticates the server to TypeSafe. They are different credentials. Generate a database token with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and use that value as the JSON key in `SDD_API_TOKENS`.

Apply the schema and tenant policies:

```bash
python -m scripts.migrate_generic
```

Complete the runtime grants as the database administrator. With Docker, open `psql` again using the command above:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sdd_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sdd_app;
REVOKE UPDATE ON source_versions, evaluator_revisions,
  decision_policy_revisions, observations, human_assertions FROM sdd_app;
REVOKE ALL ON FUNCTION sdd_reject_evidence_update() FROM PUBLIC;
\q
```

The migration creates the generic data schema, row-level security policies and immutable evidence guards. Run the service with `DATABASE_URL` pointing to `sdd_app`, never the administrator. `SDD_ADMIN_DATABASE_URL` is for setup and can be removed from the service environment afterward.

## Start the workspace

```bash
python -m sdd.cli serve
```

Open [English](http://127.0.0.1:8000/ask/en), [简体中文](http://127.0.0.1:8000/ask/zh), or the [API reference](http://127.0.0.1:8000/docs). Enter the database token in the connection settings. Import a dataset before asking a question about it.

Existing process environment variables take precedence over `.env`. The supplied `.env.example` is a template, not an active credential file. The repository excludes `.env`, runtime databases and local artifacts.

## Optional hybrid mode

Add `OPENAI_API_KEY`, set `SDD_LLM_TRANSPORT=openai`, and select an available structured-output model with `SDD_LLM_MODEL`. JEV remains responsible for context selection and review. See [hybrid configuration](HYBRID_QUERY.md#configuration) for the supported transports and call controls.

## Verify your installation

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

The tests do not call model providers. PostgreSQL checks require `SDD_TEST_POSTGRES_URL`; `python -m scripts.run_tests` reads the configured database connection and enables them. Use a dedicated local test database for this check.

If schema setup reports a missing `sdd_app` role, finish the role-creation step first. If it tries to read `.runtime/credentials.json`, set `SDD_ADMIN_DATABASE_URL` explicitly. That fallback supports an existing portable Windows installation and is not needed for a fresh Docker setup.
