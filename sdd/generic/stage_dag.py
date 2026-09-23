"""Typed relational stages, shared dependencies and deterministic CTE compilation."""

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json

from sqlglot import exp

from .relational import ratio, statistic, table
from .sql import col, literal


NUMERIC = {"integer", "number"}


@dataclass(frozen=True)
class Column:
    kind: str
    label: str
    nullable: bool = True
    unit: str | None = None
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class Term:
    op: str
    args: tuple = ()

    def columns(self):
        if self.op == "column":
            return {self.args[0]}
        return set().union(*(a.columns() for a in self.args if isinstance(a, Term)))

    def type(self, schema):
        if self.op == "column":
            if self.args[0] not in schema:
                raise ValueError(f"Stage operand is unavailable: {self.args[0]}")
            return schema[self.args[0]]
        if self.op == "literal":
            value = self.args[0]
            kind = (
                "boolean"
                if isinstance(value, bool)
                else "number"
                if isinstance(value, (int, float))
                else "text"
            )
            return Column(kind, str(value), value is None)
        children = [a.type(schema) for a in self.args if isinstance(a, Term)]
        lineage = tuple(sorted({item for child in children for item in child.lineage}))
        numeric = {
            "sum",
            "avg",
            "add",
            "sub",
            "mul",
            "div",
            "abs",
            "round",
            "power",
            "log10",
            "sqrt",
            "exp",
        }
        if self.op in numeric and any(c.kind not in NUMERIC for c in children):
            raise ValueError(f"{self.op} requires numeric operands")
        if self.op in numeric and any(c.unit == "identifier" for c in children):
            raise ValueError(
                "Identifiers can be counted or compared, but cannot be used as measured quantities"
            )
        if self.op in {"and", "or"} and any(c.kind != "boolean" for c in children):
            raise ValueError("Logical operators require Boolean operands")
        if self.op in {
            "eq",
            "ne",
            "lt",
            "le",
            "gt",
            "ge",
            "and",
            "or",
            "is_null",
            "not_null",
            "in",
            "like",
        }:
            return Column("boolean", self.op, True, lineage=lineage)
        if self.op in {
            "count",
            "count_distinct",
            "year",
            "month",
            "age_years",
            "rank",
            "dense_rank",
            "row_number",
        }:
            return Column(
                "integer",
                self.op,
                self.op not in {"count", "count_distinct", "rank", "dense_rank", "row_number"},
                "count" if self.op.startswith("count") else None,
                lineage,
            )
        if self.op in {"percent_rank", "cume_dist"}:
            return Column("number", self.op, False, lineage=lineage)
        if self.op == "json_text":
            return Column("text", "JSON scalar", True, lineage=lineage)
        if self.op in {"number", "clean_number"}:
            return Column("number", self.op, True, lineage=lineage)
        if self.op == "case":
            null_then = self.args[1].op == "literal" and self.args[1].args[0] is None
            null_else = self.args[2].op == "literal" and self.args[2].args[0] is None
            if children[0].kind != "boolean" or (
                children[1].kind != children[2].kind
                and not ({children[1].kind, children[2].kind} <= NUMERIC)
                and not (null_then or null_else)
            ):
                raise ValueError("CASE requires a Boolean condition and compatible result types")
            result_type = children[2] if null_then else children[1]
            kind = "number" if {children[1].kind, children[2].kind} == NUMERIC else result_type.kind
            return Column(kind, "conditional value", True, result_type.unit, lineage)
        if self.op in {"min", "max", "lag", "lead", "lower", "suffix"}:
            return Column(children[0].kind, self.op, True, children[0].unit, lineage)
        if self.op in numeric:
            return Column(
                "number", self.op, True, children[0].unit if len(children) == 1 else None, lineage
            )
        raise ValueError(f"Unknown stage expression: {self.op}")

    def sql(self):
        if self.op == "column":
            name = self.args[0]
            alias, name = name.split(".", 1) if name.startswith(("l.", "r.")) else (None, name)
            return col(name, alias)
        if self.op == "literal":
            return literal(self.args[0])
        nodes = [a.sql() if isinstance(a, Term) else a for a in self.args]
        if self.op in {"count", "count_distinct", "sum", "avg", "min", "max"}:
            return statistic(self.op, nodes[0] if nodes else None)
        if self.op in {"power", "log10", "sqrt", "exp"}:
            functions = {"power": exp.Pow, "sqrt": exp.Sqrt, "exp": exp.Exp}
            if self.op == "log10":
                return exp.Log(this=literal(10), expression=nodes[0])
            if self.op == "power":
                return exp.Pow(this=nodes[0], expression=nodes[1])
            return functions[self.op](this=nodes[0])
        binary = {
            "add": exp.Add,
            "sub": exp.Sub,
            "mul": exp.Mul,
            "eq": exp.EQ,
            "ne": exp.NEQ,
            "lt": exp.LT,
            "le": exp.LTE,
            "gt": exp.GT,
            "ge": exp.GTE,
            "and": exp.And,
            "or": exp.Or,
            "like": exp.Like,
        }
        if self.op in binary:
            return binary[self.op](
                this=exp.Paren(this=nodes[0]), expression=exp.Paren(this=nodes[1])
            )
        if self.op == "json_text":
            return exp.JSONExtractScalar(
                this=nodes[0],
                expression=exp.JSONPath(
                    expressions=[exp.JSONPathRoot(), exp.JSONPathKey(this=self.args[1].args[0])]
                ),
            )
        if self.op == "div":
            return ratio(*nodes)
        if self.op in {"is_null", "not_null"}:
            node = exp.Is(this=nodes[0], expression=exp.Null())
            return exp.Not(this=node) if self.op == "not_null" else node
        if self.op == "in":
            return exp.In(this=nodes[0], expressions=nodes[1:])
        if self.op == "case":
            return exp.Case(ifs=[exp.If(this=nodes[0], true=nodes[1])], default=nodes[2])
        if self.op in {"number", "clean_number"}:
            node = nodes[0]
            if self.op == "clean_number":
                node = exp.RegexpReplace(
                    this=node,
                    expression=literal(r"[^0-9.\-]"),
                    replacement=literal(""),
                )
            return exp.Cast(
                this=exp.Nullif(this=node, expression=literal("")), to=exp.DataType.build("DOUBLE")
            )
        if self.op in {"year", "month", "age_years"}:
            value = exp.Cast(this=nodes[0], to=exp.DataType.build("DATE"))
            part = exp.Extract(
                this=exp.Var(this="MONTH" if self.op == "month" else "YEAR"), expression=value
            )
            return (
                exp.Sub(
                    this=exp.Extract(this=exp.Var(this="YEAR"), expression=exp.CurrentDate()),
                    expression=part,
                )
                if self.op == "age_years"
                else part
            )
        if self.op == "suffix":
            return exp.SplitPart(this=nodes[0], delimiter=literal("_"), part_index=literal(-1))
        unary = {
            "abs": exp.Abs,
            "lower": exp.Lower,
            "round": exp.Round,
            "lag": exp.Lag,
            "lead": exp.Lead,
        }
        if self.op == "round" and len(nodes) == 2:
            return exp.Round(this=nodes[0], decimals=nodes[1])
        if self.op in unary:
            return unary[self.op](this=nodes[0])
        if self.op in {"percent_rank", "cume_dist"}:
            return {"percent_rank": exp.PercentRank, "cume_dist": exp.CumeDist}[self.op]()
        if self.op in {"rank", "dense_rank", "row_number"}:
            return {"rank": exp.Rank, "dense_rank": exp.DenseRank, "row_number": exp.RowNumber}[
                self.op
            ]()
        raise ValueError(f"Unknown stage expression: {self.op}")


def ref(name):
    return Term("column", (name,))


def value(item):
    return Term("literal", (item,))


def operation(name, *args):
    return Term(name, args)


@dataclass
class Stage:
    id: str
    operation: str
    inputs: tuple[str, ...]
    columns: dict[str, Column]
    grain: tuple[str, ...] | None
    query: exp.Select
    description: str
    keys: tuple[tuple[str, ...], ...] = ()
    assertions: list[str] = field(default_factory=list)


class StageDAG:
    def __init__(self):
        self.nodes = {}
        self._equivalent = {}

    def _add(self, kind, inputs, schema, grain, query, description, keys=(), assertions=()):
        if any(node not in self.nodes for node in inputs):
            raise ValueError("A stage dependency is missing or cyclic")
        if not schema:
            raise ValueError("A stage must expose at least one typed column")
        fingerprint = sha256(
            json.dumps(
                [kind, inputs, query.sql(), {k: asdict(v) for k, v in schema.items()}, grain, keys],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if fingerprint in self._equivalent:
            return self._equivalent[fingerprint]
        identity = "stage_" + str(len(self.nodes))
        self.nodes[identity] = Stage(
            identity,
            kind,
            tuple(inputs),
            schema,
            grain,
            query,
            description,
            tuple(keys),
            list(assertions),
        )
        self._equivalent[fingerprint] = identity
        return identity

    def source(
        self,
        query,
        schema,
        *,
        grain=None,
        keys=(),
        description="Authorized source population",
        bindings=None,
    ):
        if not isinstance(query, exp.Select) or len(query.expressions) != len(schema):
            raise ValueError("A source stage must be a typed SELECT with matching outputs")
        bindings = bindings or {}
        query = query.copy()
        for logical, stage in bindings.items():
            if stage not in self.nodes:
                raise ValueError("Unknown source dependency")
            matches = [t for t in query.find_all(exp.Table) if t.name == logical]
            if not matches:
                raise ValueError("Source binding is not used by this query")
            for relation in matches:
                relation.set("this", exp.to_identifier(stage, quoted=True))
        return self._add(
            "source",
            tuple(dict.fromkeys(bindings.values())),
            schema,
            grain,
            query,
            description,
            keys,
        )

    def _select(self, source):
        node = self.nodes[source]
        return exp.select(*[col(key) for key in node.columns]).from_(table(source, source))

    def filter(self, source, predicate, description="Restrict this stage's population"):
        node = self.nodes[source]
        if predicate.type(node.columns).kind != "boolean":
            raise ValueError("A filter must be Boolean")
        if any(isinstance(part, (exp.AggFunc, exp.Window)) for part in predicate.sql().walk()):
            raise ValueError("Filter aggregates/windows only after their own stage")
        return self._add(
            "filter",
            (source,),
            node.columns,
            node.grain,
            self._select(source).where(predicate.sql()),
            description,
            node.keys,
        )

    def project(
        self,
        source,
        expressions,
        *,
        description="Derive stage values",
        keep=False,
        distinct=False,
        order=(),
        limit=None,
    ):
        node = self.nodes[source]
        expressions = {**({key: ref(key) for key in node.columns} if keep else {}), **expressions}
        schema = {key: term.type(node.columns) for key, term in expressions.items()}
        if any(
            isinstance(part, (exp.AggFunc, exp.Window))
            for term in expressions.values()
            for part in term.sql().walk()
        ):
            raise ValueError("Projection cannot hide an aggregate or window stage")
        query = exp.select(
            *[exp.alias_(term.sql(), key, quoted=True) for key, term in expressions.items()]
        ).from_(table(source, source))
        if any(key not in node.columns for key, _ in order):
            raise ValueError("Projection order refers to a missing source column")
        visible = {term.args[0] for term in expressions.values() if term.op == "column"}
        if distinct and any(set(key) <= visible for key in node.keys):
            distinct = False
        if distinct and any(
            key not in {term.args[0] for term in expressions.values() if term.op == "column"}
            for key, _ in order
        ):
            raise ValueError("Distinct outputs cannot order by an omitted value")
        if order:
            query = query.order_by(
                *[
                    exp.Ordered(this=col(key), desc=descending, nulls_first=False)
                    for key, descending in order
                ]
            )
        if limit is not None:
            if not isinstance(limit, int) or not 1 <= limit <= 100000:
                raise ValueError("Invalid projection limit")
            query = query.limit(limit)
        if distinct:
            query = query.distinct()
        renamed = {term.args[0]: key for key, term in expressions.items() if term.op == "column"}
        keys = tuple(
            tuple(renamed[k] for k in unique)
            for unique in node.keys
            if all(k in renamed for k in unique)
        )
        if distinct:
            keys = (*keys, tuple(schema))
        grain = (
            tuple(renamed[k] for k in node.grain)
            if node.grain is not None and all(k in renamed for k in node.grain)
            else None
        )
        return self._add("project", (source,), schema, grain, query, description, keys)

    def aggregate(self, source, groups, metrics, description="One row per grouping key"):
        node = self.nodes[source]
        if any(key not in node.columns for key in groups) or set(groups) & set(metrics):
            raise ValueError("Invalid aggregate grouping/output keys")
        for term in metrics.values():
            if term.op not in {"count", "count_distinct", "sum", "avg", "min", "max"}:
                raise ValueError("Aggregate outputs must use an explicit aggregate operator")
            for arg in term.args:
                if isinstance(arg, Term) and any(
                    isinstance(p, (exp.AggFunc, exp.Window)) for p in arg.sql().walk()
                ):
                    raise ValueError("Nested aggregate requires another stage")
        schema = {
            **{key: node.columns[key] for key in groups},
            **{key: term.type(node.columns) for key, term in metrics.items()},
        }
        query = exp.select(
            *[col(key) for key in groups],
            *[exp.alias_(term.sql(), key, quoted=True) for key, term in metrics.items()],
        ).from_(table(source, source))
        if groups:
            query = query.group_by(*[col(key) for key in groups])
        return self._add(
            "aggregate",
            (source,),
            schema,
            tuple(groups),
            query,
            description,
            (tuple(groups),),
            ("Grouping keys uniquely identify each output row",),
        )

    def window(
        self,
        source,
        name,
        term,
        *,
        partition=(),
        order=(),
        frame=None,
        description="Calculate within an ordered partition",
    ):
        return self.windows(
            source,
            {name: {"term": term, "partition": partition, "order": order, "frame": frame}},
            description=description,
        )

    @staticmethod
    def _window_expression(columns, name, term, partition=(), order=(), frame=None):
        if name in columns or any(
            key not in columns for key in [*partition, *(k for k, _ in order)]
        ):
            raise ValueError("Invalid window output or partition/order key")
        if term.op not in {
            "rank",
            "dense_rank",
            "row_number",
            "percent_rank",
            "cume_dist",
            "lag",
            "lead",
            "sum",
            "avg",
            "min",
            "max",
            "count",
        }:
            raise ValueError("Unsupported window operator")
        if (
            term.op
            in {"rank", "dense_rank", "row_number", "percent_rank", "cume_dist", "lag", "lead"}
            and not order
        ):
            raise ValueError("An ordered window needs an explicit order")
        kind = term.type(columns)
        expression = exp.Window(this=term.sql(), partition_by=[col(k) for k in partition])
        if order:
            expression.set(
                "order",
                exp.Order(
                    expressions=[
                        exp.Ordered(this=col(k), desc=desc, nulls_first=False) for k, desc in order
                    ]
                ),
            )
        if frame is not None:
            before, after = frame
            if not all(isinstance(n, int) and 0 <= n <= 10000 for n in frame):
                raise ValueError("A window frame requires bounded non-negative offsets")
            expression.set(
                "spec",
                exp.WindowSpec(
                    kind="ROWS",
                    start=literal(before) if before else "CURRENT ROW",
                    start_side="PRECEDING" if before else None,
                    end=literal(after) if after else "CURRENT ROW",
                    end_side="FOLLOWING" if after else None,
                ),
            )
        return expression, kind

    def windows(self, source, specifications, description="Independent windows on one population"):
        node = self.nodes[source]
        schema, expressions = dict(node.columns), []
        for name, specification in specifications.items():
            expression, kind = self._window_expression(node.columns, name, **specification)
            schema[name] = kind
            expressions.append(exp.alias_(expression, name, quoted=True))
        return self._add(
            "window",
            (source,),
            schema,
            node.grain,
            self._select(source).select(*expressions),
            description,
            node.keys,
        )

    def row_identity(self, source, name="_population_row"):
        """Statement-local identity for rejoining different population branches."""
        node = self.nodes[source]
        if name in node.columns:
            raise ValueError("Population row identity already exists")
        query = self._select(source).select(
            exp.alias_(exp.Window(this=exp.RowNumber()), name, quoted=True)
        )
        return self._add(
            "row_identity",
            (source,),
            {
                **node.columns,
                name: Column("integer", "Statement-local row identity", False, "identifier"),
            },
            node.grain,
            query,
            "Share row identity before branching; preserve duplicate rows",
            (*node.keys, (name,)),
            ("Identity is unique within this statement, not a business identifier",),
        )

    def latest(self, source, *, partition, order):
        if not partition or not order:
            raise ValueError("Latest-record selection needs entity keys and chronology")
        columns = self.nodes[source].columns
        marker = "_latest_position"
        while marker in columns:
            marker += "_"
        ranked = self.window(
            source, marker, operation("row_number"), partition=partition, order=order
        )
        chosen = self.filter(ranked, operation("eq", ref(marker), value(1)))
        query = exp.select(*[col(key) for key in columns]).from_(table(chosen, chosen))
        return self._add(
            "latest",
            (chosen,),
            columns,
            tuple(partition),
            query,
            "One observation per entity before parent joins",
            (tuple(partition),),
            ("Chronology and tie breakers determine the selected observation",),
        )

    def join(
        self, left, right, on, *, outputs, how="inner", description="Merge compatible stage grains"
    ):
        a, b = self.nodes[left], self.nodes[right]
        if how not in {"inner", "left", "cross"}:
            raise ValueError("Unsupported stage merge")
        left_keys, right_keys = tuple(k[0] for k in on), tuple(k[1] for k in on)
        if any(k not in a.columns for k in left_keys) or any(
            k not in b.columns for k in right_keys
        ):
            raise ValueError("Merge key is absent from its stage")
        if not any(set(unique).issubset(right_keys) for unique in b.keys):
            raise ValueError(
                "Stage merge would multiply rows: right side is not unique on merge keys"
            )
        if how == "cross" and (() not in b.keys or on):
            raise ValueError("Cross merge requires a scalar right stage")
        if not on and how != "cross":
            raise ValueError("Non-scalar merge needs explicit keys")
        schema = {
            **{"l." + k: v for k, v in a.columns.items()},
            **{
                "r." + k: Column(v.kind, v.label, v.nullable or how == "left", v.unit, v.lineage)
                for k, v in b.columns.items()
            },
        }
        projected = {key: term.type(schema) for key, term in outputs.items()}
        predicate = (
            exp.and_(
                *[
                    exp.EQ(this=col(left_key, "l"), expression=col(right_key, "r"))
                    for left_key, right_key in on
                ]
            )
            if on
            else None
        )
        query = (
            exp.select(*[exp.alias_(term.sql(), key, quoted=True) for key, term in outputs.items()])
            .from_(table(left, "l"))
            .join(table(right, "r"), on=predicate, join_type=how.upper())
        )
        renamed = {
            term.args[0][2:]: key
            for key, term in outputs.items()
            if term.op == "column" and term.args[0].startswith("l.")
        }
        keys = tuple(
            tuple(renamed[k] for k in unique)
            for unique in a.keys
            if all(k in renamed for k in unique)
        )
        grain = (
            tuple(renamed[k] for k in a.grain)
            if a.grain is not None and all(k in renamed for k in a.grain)
            else None
        )
        return self._add(
            "join",
            (left, right),
            projected,
            grain,
            query,
            description,
            keys,
            ("Right branch is unique at merge grain; no extra left rows",),
        )

    def finish(self, source, *, order=(), limit=None):
        node = self.nodes[source]
        if any(key not in node.columns for key, _ in order):
            raise ValueError("Output order refers to a missing column")
        query = self._select(source)
        if order:
            query = query.order_by(
                *[exp.Ordered(this=col(k), desc=desc, nulls_first=False) for k, desc in order]
            )
        if limit is not None:
            if not isinstance(limit, int) or not 1 <= limit <= 100000:
                raise ValueError("Invalid result limit")
            query = query.limit(limit)
        return self._add(
            "order_limit",
            (source,),
            node.columns,
            node.grain,
            query,
            "Order/limit the completed result",
            node.keys,
        )

    def layers(self, target):
        reachable = set()

        def visit(node):
            if node not in reachable:
                reachable.add(node)
                for parent in self.nodes[node].inputs:
                    visit(parent)

        visit(target)
        remaining, done, levels = set(reachable), set(), []
        while remaining:
            ready = [
                key
                for key in self.nodes
                if key in remaining and set(self.nodes[key].inputs) <= done
            ]
            if not ready:
                raise ValueError("Cyclic stage graph")
            levels.append(ready)
            done.update(ready)
            remaining.difference_update(ready)
        return levels

    def compile(self, target):
        query = self.nodes[target].query.copy()
        for level in self.layers(target):
            for identity in level:
                if identity == target:
                    continue
                query = query.with_(
                    exp.to_identifier(identity, quoted=True), as_=self.nodes[identity].query.copy()
                )
        return query

    def describe(self, target):
        layers = self.layers(target)
        uses = {key: 0 for level in layers for key in level}
        for key in uses:
            for parent in self.nodes[key].inputs:
                uses[parent] += 1
        return {
            "version": 1,
            "target": target,
            "layers": layers,
            "max_independent_stages": max(map(len, layers)),
            "shared_stages": [key for key, count in uses.items() if count > 1],
            "stages": [
                {
                    "id": node.id,
                    "operator": node.operation,
                    "inputs": list(node.inputs),
                    "grain": node.grain,
                    "keys": node.keys,
                    "columns": {k: asdict(v) for k, v in node.columns.items()},
                    "description": node.description,
                    "assertions": node.assertions,
                    "sql": node.query.sql(dialect="postgres"),
                }
                for level in layers
                for key in level
                for node in [self.nodes[key]]
            ],
        }
