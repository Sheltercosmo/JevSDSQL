import os
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from sdd.db import Database
from sdd.generic import schema as catalog_schema
from sdd.operators import schema
from sdd.operators.service import OperatorService
from test_operator_runtime import Model


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_POSTGRES_URL"), reason="PostgreSQL test URL not configured"
)


def test_operator_postgres_rls_immutability_and_row_cache():
    db = Database(os.environ["SDD_TEST_POSTGRES_URL"])
    tenant = "operator-test-" + uuid.uuid4().hex
    model = Model()
    service = OperatorService(db, model, tenant, "reviewer", "reviewer")
    dataset = service.catalog.create(
        tenant, "Notes", [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}], primary_key=["id"]
    )
    try:
        args = {"subjects": {"dataset_id": dataset["id"]}, "concept_revs": ["p"]}
        first = service.call("TAG", args)
        assert first["output_state"] == "VALUE"
        observation = next(iter(first["observations"].values()))["observation_id"]
        with db.transaction(tenant + "-other") as connection:
            assert connection.execute(select(schema.observations)).all() == []
            assert connection.execute(select(schema.runs)).all() == []
        with pytest.raises(DBAPIError), db.transaction(tenant) as connection:
            connection.execute(
                update(schema.observations)
                .where(schema.observations.c.id == observation)
                .values(answer={"type": "noul", "noul": 0})
            )
        with db.transaction(tenant) as connection:
            table = service.catalog.table(dataset, connection)
            connection.execute(update(table).where(table.c.id == 1).values(text="Changed"))
        second = service.call("TAG", args)
        assert second["output_state"] == "VALUE" and second["manifest"]["reserved_judgments"] == 1
        assert model.calls == 3
    finally:
        with db.transaction(tenant) as connection:
            for table in (
                schema.subscriptions,
                schema.generations,
                schema.approvals,
                schema.assertions,
                schema.invalidations,
                schema.attempts,
                schema.observations,
                schema.definitions,
                schema.runs,
            ):
                connection.execute(delete(table).where(table.c.tenant == tenant))
            service.catalog.table(dataset, connection).drop(connection)
            connection.execute(
                delete(catalog_schema.datasets).where(catalog_schema.datasets.c.tenant == tenant)
            )
        db.engine.dispose()
