"""Exact decimal reducers exposed as SQL aggregates for bounded label snapshots."""

import sqlite3
from decimal import Decimal, localcontext


def aggregate(kind, values, decimal_output=False):
    if kind not in {"count", "sum", "avg", "min", "max"}:
        raise ValueError("Unsupported aggregate")
    numbers = [Decimal(str(v)) for v in values if v is not None]
    if any(not v.is_finite() for v in numbers):
        raise ValueError("Aggregate operands must be finite")
    precision = (
        max((len(v.as_tuple().digits) + abs(v.as_tuple().exponent) for v in numbers), default=1)
        + len(str(len(numbers)))
        + 30
    )

    class Reducer:
        def __init__(self):
            self.total, self.count, self.extreme = Decimal(0), 0, None

        def step(self, value):
            if value is None:
                return
            number = Decimal(value)
            with localcontext() as context:
                context.prec = precision
                self.total += number
            self.count += 1
            if self.extreme is None:
                self.extreme = number
            elif kind == "min":
                self.extreme = min(self.extreme, number)
            elif kind == "max":
                self.extreme = max(self.extreme, number)

        def finalize(self):
            if not self.count:
                return None
            with localcontext() as context:
                context.prec = precision
                if kind == "avg":
                    value = self.total / self.count
                elif kind in {"min", "max"}:
                    value = self.extreme
                else:
                    value = self.total
                return str(value)

    with sqlite3.connect(":memory:") as connection:
        connection.create_aggregate("exact_value", 1, Reducer)
        connection.execute("CREATE TABLE operands (value TEXT)")
        connection.executemany("INSERT INTO operands VALUES (?)", [(str(v),) for v in numbers])
        expression = "COUNT(value)" if kind == "count" else "exact_value(value)"
        raw = connection.execute(f"SELECT {expression} FROM operands").fetchone()[0]
    if raw is None:
        return None
    if kind == "count" or kind != "avg" and all(type(v) is int for v in values):
        return int(Decimal(raw))
    return str(raw) if decimal_output else float(raw)
