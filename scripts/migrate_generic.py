"""Additive generic migration; create generic catalog/evidence tables and a data schema."""

import json
import os
from sdd.config import load_env
from pathlib import Path
from sqlalchemy import text
from sdd.db import Database
from sdd.generic import schema
from sdd.operators import schema as operator_schema
from scripts.postgres_security import secure


def main():
    load_env()
    admin_url = os.getenv("SDD_ADMIN_DATABASE_URL")
    if not admin_url:
        credentials = json.loads(Path(".runtime/credentials.json").read_text(encoding="utf-8"))
        admin_url = f"postgresql+psycopg://sdd_admin:{credentials['admin']}@127.0.0.1:55432/sdd"
    db = Database(admin_url)
    db.initialize()
    secure(db)
    with db.engine.begin() as cx:
        cx.execute(text("CREATE SCHEMA IF NOT EXISTS sdd_data AUTHORIZATION sdd_app"))
        cx.execute(text("GRANT USAGE, CREATE ON SCHEMA sdd_data TO sdd_app"))
        for table in (
            schema.datasets,
            schema.evidence,
            schema.attempts,
            schema.runs,
            schema.query_history,
            schema.previews,
            schema.row_versions,
            schema.features,
            schema.payloads,
            schema.inference_calls,
            schema.feature_reviews,
            schema.maintenance_jobs,
            operator_schema.observations,
            operator_schema.invalidations,
            operator_schema.runs,
            operator_schema.attempts,
            operator_schema.assertions,
            operator_schema.definitions,
            operator_schema.approvals,
            operator_schema.generations,
            operator_schema.subscriptions,
        ):
            cx.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table.name} TO sdd_app"))
    from sdd.generic.history import QueryHistory

    principals = json.loads(os.getenv("SDD_API_TOKENS", "{}")).values()
    imported = sum(
        QueryHistory(db).import_owned_runs(tenant, actor)
        for tenant, actor in {(p["tenant"], p["name"]) for p in principals}
    )
    print(
        f"Generic schema and tenant RLS installed; {imported} owned queries added to history. Existing source/evidence tables preserved."
    )


if __name__ == "__main__":
    main()
