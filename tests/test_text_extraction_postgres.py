"""PostgreSQL exact decimals and concurrent replay-safe text-import commits."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import uuid

import pytest
from sqlalchemy import delete

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.text_import import TextImporter
from sdd.generic import schema as catalog_schema
from sdd.operators import schema
from test_text_extraction import ExtractModel

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_POSTGRES_URL"), reason="PostgreSQL test URL not configured"
)


def test_decimal_and_concurrent_commit():
    db = Database(os.environ["SDD_TEST_POSTGRES_URL"])
    tenant = "text-import-test-" + uuid.uuid4().hex
    catalog = Catalog(db)
    importer = TextImporter(
        db, ExtractModel([{"anchor": "measurement", "v": "9007199254740993.01"}])
    )
    try:
        preview = importer.preview(
            tenant,
            "owner",
            name="precise",
            text="A measurement of 9007199254740993.01 was recorded.",
            row_description="One measurement",
            record_mode="document",
            columns=[{"name": "v", "type": "number", "description": "Recorded measurement"}],
        )
        assert preview["can_import"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: importer.commit(tenant, "owner", preview["preview_token"]), range(2)
                )
            )
        assert sum(r["replayed"] for r in results) == 1
        rows = catalog.rows(tenant, catalog.get(tenant, results[0]["dataset_id"]))
        assert len(rows) == 1 and rows[0]["v"] == Decimal("9007199254740993.01")
        assert catalog.list(tenant + "-other") == []
    finally:
        targets = catalog.list(tenant)
        with db.transaction(tenant) as connection:
            for target in targets:
                catalog.table(target, connection).drop(connection)
            for table in (
                catalog_schema.datasets,
                catalog_schema.previews,
                schema.attempts,
                schema.observations,
                schema.runs,
            ):
                connection.execute(delete(table).where(table.c.tenant == tenant))
        db.engine.dispose()
