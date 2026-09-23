from sqlalchemy import case, exists, insert, select, update

from ..ledger import Ledger, digest, now, uid
from . import schema


class Store:
    def __init__(self, db, tenant, actor):
        self.db, self.tenant, self.actor = db, tenant, actor
        self.ledger = Ledger(db)

    def add(self, table, connection=None, **values):
        row = {"id": uid(), "tenant": self.tenant, **values, "created_at": now()}
        if connection is not None:
            connection.execute(insert(table).values(**row))
        else:
            with self.db.transaction(self.tenant) as connection:
                connection.execute(insert(table).values(**row))
        return row

    def get(self, table, identity):
        return self.ledger.get(self.tenant, table, identity)

    def cached(self, key):
        with self.db.transaction(self.tenant) as connection:
            row = (
                connection.execute(
                    select(schema.observations)
                    .where(
                        schema.observations.c.tenant == self.tenant,
                        schema.observations.c.cache_key == key,
                        ~exists(
                            select(schema.invalidations.c.id).where(
                                schema.invalidations.c.tenant == self.tenant,
                                schema.invalidations.c.observation_id == schema.observations.c.id,
                            )
                        ),
                    )
                    .order_by(
                        schema.observations.c.created_at.desc(), schema.observations.c.id.desc()
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def start(self, operator, request):
        return self.add(
            schema.runs,
            actor=self.actor,
            operator=operator,
            plan_hash=digest(request),
            request=request,
            result={},
            state="RUNNING",
        )["id"]

    def finish(self, identity, result):
        with self.db.transaction(self.tenant) as connection:
            connection.execute(
                update(schema.runs)
                .where(
                    schema.runs.c.tenant == self.tenant,
                    schema.runs.c.id == identity,
                )
                .values(
                    result=result,
                    state=case(
                        (schema.runs.c.state == "CANCELLED", "CANCELLED"),
                        else_=result["operation_state"],
                    ),
                )
            )

    def cancelled(self, identity, connection=None):
        if connection is None:
            return self.get(schema.runs, identity)["state"] == "CANCELLED"
        return (
            connection.execute(
                select(schema.runs.c.state).where(
                    schema.runs.c.id == identity, schema.runs.c.tenant == self.tenant
                )
            ).scalar_one()
            == "CANCELLED"
        )

    def invalidate(self, source_revisions, reason):
        changed = []
        with self.db.transaction(self.tenant) as connection:
            rows = (
                connection.execute(
                    select(schema.observations).where(schema.observations.c.tenant == self.tenant)
                )
                .mappings()
                .all()
            )
            for row in rows:
                if set(source_revisions).intersection(row["source_revisions"]):
                    connection.execute(
                        insert(schema.invalidations).values(
                            id=uid(),
                            tenant=self.tenant,
                            observation_id=row["id"],
                            reason=reason,
                            created_at=now(),
                        )
                    )
                    changed.append(row["id"])
        return changed
