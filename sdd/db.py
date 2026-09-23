from contextlib import contextmanager, nullcontext
from threading import RLock
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool
from .schema import metadata


class Database:
    def __init__(self, url):
        self._transaction_lock = RLock()
        options = {}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                from .sqlite_functions import register

                register(connection)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

    def initialize(self):
        from .generic import schema as generic_schema  # noqa: F401 - register generic tables

        from .operators import schema as operator_schema  # noqa: F401 - register operator tables

        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, tenant, isolation_level=None):
        if not tenant or len(tenant) > 100:
            raise ValueError("A tenant identity is required")
        engine = (
            self.engine.execution_options(isolation_level=isolation_level)
            if isolation_level
            else self.engine
        )
        guard = self._transaction_lock if self.engine.dialect.name == "sqlite" else nullcontext()
        with guard, engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.execute(
                    text("SELECT set_config('sdd.tenant', :tenant, true)"), {"tenant": tenant}
                )
            yield conn
