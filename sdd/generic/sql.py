"""SQLGlot-backed SQL guard, semantic operator lowering, and controlled DML.

Logical table identifiers are resolved only through the authenticated tenant's catalog.
All scalar literals are bound parameters, never interpolated into executable SQL.
"""

import re
import time
from collections import Counter
from decimal import Decimal
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema
from sqlglot.optimizer.scope import traverse_scope
from sqlalchemy import text, insert, update, delete, select
from ..ledger import uid, now, digest
from .catalog import Catalog, serial
from .semantics import Semantics
from . import schema as schema
from .query_rewrites import decorrelate_scalar_aggregate

FUNCTIONS = {
    "EXISTS",
    "ANY",
    "ALL",
    "AND",
    "OR",
    "NOT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "COUNT",
    "COALESCE",
    "NULLIF",
    "ROUND",
    "ABS",
    "FLOOR",
    "CEIL",
    "CEILING",
    "LOWER",
    "UPPER",
    "LENGTH",
    "CHAR_LENGTH",
    "CONCAT",
    "CONCAT_WS",
    "SUBSTRING",
    "TRIM",
    "LTRIM",
    "RTRIM",
    "REPLACE",
    "REGEXP_REPLACE",
    "SPLIT_PART",
    "LEFT",
    "RIGHT",
    "CAST",
    "TRY_CAST",
    "CASE",
    "IF",
    "EXTRACT",
    "DATE_TRUNC",
    "TIMESTAMP_TRUNC",
    "DATE",
    "TIME",
    "TIMESTAMP",
    "TO_CHAR",
    "TO_DATE",
    "TO_TIMESTAMP",
    "DATE_ADD",
    "DATE_SUB",
    "DATEDIFF",
    "STRFTIME",
    "TIME_TO_STR",
    "STR_TO_DATE",
    "STR_TO_TIME",
    "TS_OR_DS_TO_DATE",
    "TS_OR_DS_TO_TIMESTAMP",
    "ROW_NUMBER",
    "RANK",
    "DENSE_RANK",
    "PERCENT_RANK",
    "CUME_DIST",
    "LAG",
    "LEAD",
    "FIRST_VALUE",
    "LAST_VALUE",
    "NTH_VALUE",
    "NTILE",
    "PERCENTILE_CONT",
    "PERCENTILE_DISC",
    "STDDEV",
    "STDDEV_SAMP",
    "STDDEV_POP",
    "VARIANCE",
    "VAR_SAMP",
    "VAR_POP",
    "POWER",
    "SQRT",
    "LOG",
    "LN",
    "EXP",
    "MOD",
    "BOOL_AND",
    "BOOL_OR",
    "LOGICAL_AND",
    "LOGICAL_OR",
    "ARRAY_AGG",
    "STRING_AGG",
    "GROUP_CONCAT",
    "GREATEST",
    "LEAST",
    "SEMANTIC",
    "SEMANTIC_FEATURE",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "CURRENT_TIME",
    "JSON_EXTRACT",
    "JSON_EXTRACT_SCALAR",
    "JSONB_EXTRACT",
    "JSONB_EXTRACT_SCALAR",
}


def literal(value):
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float, Decimal)):
        return exp.Literal.number(str(value))
    return exp.Literal.string(str(value))


def col(name, table=None):
    return exp.column(name, table=table, quoted=True)


class SQLService:
    def __init__(self, db, decisions=None):
        self.db, self.catalog, self.decisions = db, Catalog(db), decisions
        self.semantics = Semantics(db, decisions) if decisions else None

    def prepare(self, tenant, sql):
        if not isinstance(sql, str) or len(sql) > 30000:
            raise ValueError("SQL input must be at most 30,000 characters")
        trees = sqlglot.parse(sql, read="postgres")
        if len(trees) != 1 or trees[0] is None:
            raise ValueError("Exactly one SQL statement is required")
        tree = trees[0]
        if not isinstance(
            tree,
            (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Update, exp.Delete, exp.Insert),
        ):
            raise ValueError(
                "Only SELECT, INSERT, UPDATE, and DELETE are supported; DDL is not query data"
            )
        if len(list(tree.walk())) > 2000:
            raise ValueError("SQL expression exceeds the complexity budget")
        for node in tree.walk():
            if isinstance(node, (exp.Command, exp.Into, exp.Placeholder, exp.Parameter, exp.Lock)):
                raise ValueError(
                    "Commands, SELECT INTO, parameters, and explicit locks are not allowed"
                )
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete)) and node is not tree:
                raise ValueError("Nested data-modifying statements are not allowed")
            if isinstance(node, exp.Func):
                if isinstance(node.parent, exp.Dot):
                    raise ValueError("Schema-qualified functions are not supported")
                name = (
                    node.name.upper()
                    if isinstance(node, exp.Anonymous)
                    else node.sql_name().upper()
                )
                if name not in FUNCTIONS:
                    raise ValueError("Function not in the safe calculation catalog: " + name)
            if isinstance(node, exp.DataType) and (
                node.this == exp.DataType.Type.USERDEFINED
                or any(x in node.sql().upper() for x in ("REGCLASS", "REGPROC", "USER-DEFINED"))
            ):
                raise ValueError("This cast target is not supported")
        datasets = self.catalog.list(tenant)
        by_name = {d["name"]: d for d in datasets}

        def lookup(node):
            if node.db or node.catalog:
                raise ValueError(
                    "Use logical dataset names, without database or schema qualification"
                )
            found = by_name.get(node.name)
            if not found:
                choices = [d for d in datasets if d["name"].casefold() == node.name.casefold()]
                found = choices[0] if len(choices) == 1 else None
            if not found:
                raise ValueError("Unknown or unauthorized dataset: " + node.name)
            return found

        bindings = {}
        references = []
        target = None
        is_read = isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except))
        if is_read:
            for scope in traverse_scope(tree):
                for _, (_, source) in scope.selected_sources.items():
                    if isinstance(source, exp.Table):
                        dataset = lookup(source)
                        bindings[id(source)] = dataset
                        references.append(dataset)
            if not references:
                raise ValueError("A query must reference an authorized dataset")
            schema = {
                d["name"]: {
                    **{
                        c["name"]: {
                            "text": "TEXT",
                            "integer": "BIGINT",
                            "number": "DECIMAL",
                            "boolean": "BOOLEAN",
                            "date": "DATE",
                            "datetime": "TIMESTAMPTZ",
                            "json": "JSON",
                        }[c["type"]]
                        for c in d["columns"]
                    },
                    **({"_sdd_row_id": "TEXT"} if d["primary_key"] == ["_sdd_row_id"] else {}),
                }
                for d in references
            }
            for node in tree.find_all(exp.Table):
                if not node.alias and node.this.args.get("quoted"):
                    node.set(
                        "alias", exp.TableAlias(this=exp.to_identifier(node.name, quoted=True))
                    )
            tree = qualify(
                tree,
                dialect="postgres",
                schema=MappingSchema(schema, dialect="postgres", normalize=False),
                identify=True,
                validate_qualify_columns=True,
            )
            # Qualification returns copied nodes, so bind again using lexical SQL scopes.
            if self.db.engine.dialect.name == "postgresql":
                tree = decorrelate_scalar_aggregate(tree)
            bindings = {}
            for scope in traverse_scope(tree):
                for _, (_, source) in scope.selected_sources.items():
                    if isinstance(source, exp.Table):
                        bindings[id(source)] = lookup(source)
        else:
            if tree.args.get("with_") or tree.find(exp.Select):
                raise ValueError("Mutation subqueries require a separately reviewed SQL migration")
            target_node = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if not isinstance(target_node, exp.Table):
                raise ValueError("Invalid mutation target")
            target = lookup(target_node)
            if not target["writable"]:
                raise ValueError("This dataset is read-only")
            if tree.args.get("from_") or tree.args.get("using") or tree.args.get("joins"):
                raise ValueError("Mutations currently support one target table")
            names = {c["name"] for c in target["columns"]} | set(target["primary_key"])
            for column in tree.find_all(exp.Column):
                if column.name not in names:
                    raise ValueError("Unknown column: " + column.name)
                if column.table and column.table not in (target_node.alias_or_name, target["name"]):
                    raise ValueError("Invalid mutation qualifier")
            if isinstance(tree, exp.Update):
                for assignment in tree.expressions:
                    if not isinstance(assignment, exp.EQ) or not isinstance(
                        assignment.this, exp.Column
                    ):
                        raise ValueError("Invalid assignment")
                    if assignment.this.name in target["primary_key"]:
                        raise ValueError("Primary-key changes require explicit data migration")
                    assignment.this.set("table", None)
            if isinstance(tree, exp.Insert):
                if not isinstance(tree.this, exp.Schema) or not isinstance(
                    tree.expression, exp.Values
                ):
                    raise ValueError("INSERT requires named columns and VALUES")
                keys = [c.name for c in tree.this.expressions]
                if len(set(keys)) != len(keys) or set(keys) - names or "_sdd_row_id" in keys:
                    raise ValueError("Invalid insert columns")
                if (
                    tree.args.get("conflict")
                    or tree.args.get("overwrite")
                    or tree.args.get("returning")
                ):
                    raise ValueError("Use plain INSERT VALUES")
            references = [target]
            bindings[id(target_node)] = target
        return tree, bindings, list({d["id"]: d for d in references}.values()), target

    def bind(self, tree, bindings):
        tree = tree.copy()
        # Bind copied table nodes by their still-logical table names; CTE aliases are scoped.
        authorized = {d["name"].casefold(): d for d in bindings.values()}
        read = isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except))
        tables = []
        if read:
            for scope in traverse_scope(tree):
                tables.extend(
                    source
                    for _, source in scope.selected_sources.values()
                    if isinstance(source, exp.Table)
                )
        else:
            tables = [tree.this.this if isinstance(tree.this, exp.Schema) else tree.this]
        for node in tables:
            dataset = authorized[node.name.casefold()]
            logical = node.name
            if not node.alias and not isinstance(tree, exp.Insert):
                node.set("alias", exp.TableAlias(this=exp.to_identifier(logical, quoted=True)))
            node.set("this", exp.to_identifier(dataset["table_name"], quoted=True))
            node.set(
                "db",
                exp.to_identifier(dataset["schema_name"], quoted=True)
                if dataset["schema_name"]
                else None,
            )
        params, semantic_bindings = {}, {}

        def parameter(node):
            binding = node.meta.get("semantic_binding")
            if binding:
                if binding not in semantic_bindings:
                    expression = node.copy()
                    expression.meta.pop("semantic_binding")
                    semantic_bindings[binding] = expression.transform(parameter)
                return semantic_bindings[binding].copy()
            if isinstance(node, exp.Literal):
                if node.find_ancestor(exp.DataType):
                    return node
                if not node.is_string and isinstance(node.parent, (exp.Ordered, exp.Group)):
                    return node
                key = "p" + str(len(params))
                params[key] = node.this if node.is_string else Decimal(node.this)
                if not node.is_string and re.fullmatch(r"[0-9]+", node.this):
                    params[key] = int(params[key])
                return exp.Placeholder(this=key)
            return node

        if self.db.engine.dialect.name == "sqlite":

            def lower_extract(node):
                if not isinstance(node, exp.Extract):
                    return node
                formats = {
                    "YEAR": "%Y",
                    "MONTH": "%m",
                    "DAY": "%d",
                    "HOUR": "%H",
                    "MINUTE": "%M",
                    "SECOND": "%S",
                }
                part = node.this.name.upper()
                if part not in formats:
                    raise ValueError("Unsupported SQLite date part: " + part)
                return exp.Cast(
                    this=exp.TimeToStr(this=node.expression.copy(), format=literal(formats[part])),
                    to=exp.DataType.build("INTEGER"),
                )

            def lower_json(node):
                if isinstance(node, exp.Cast) and node.to.this in (
                    exp.DataType.Type.JSON,
                    exp.DataType.Type.JSONB,
                ):
                    return node.this.copy()
                if not isinstance(node, (exp.JSONBExtract, exp.JSONBExtractScalar)):
                    return node
                path = node.expression
                if (
                    not isinstance(path, exp.Literal)
                    or not path.is_string
                    or not path.this.startswith("{")
                    or not path.this.endswith("}")
                ):
                    raise ValueError("SQLite JSON paths require a literal PostgreSQL path array")
                import csv

                keys = next(csv.reader([path.this[1:-1]], escapechar="\\"))
                parts = [exp.JSONPathRoot()]
                for key in keys:
                    parts.append(
                        exp.JSONPathSubscript(this=int(key))
                        if key.isdigit()
                        else exp.JSONPathKey(this=key)
                    )
                kind = (
                    exp.JSONExtractScalar
                    if isinstance(node, exp.JSONBExtractScalar)
                    else exp.JSONExtract
                )
                return kind(this=node.this.copy(), expression=exp.JSONPath(expressions=parts))

            tree = tree.transform(lower_json).transform(lower_json)
            tree = tree.transform(lower_extract)

        tree = tree.transform(parameter)
        dialect = "postgres" if self.db.engine.dialect.name == "postgresql" else "sqlite"
        sql = tree.sql(dialect=dialect)
        sql = re.sub(r"%\((p\d+)\)s", r":\1", sql)
        if dialect == "sqlite":
            params = {k: float(v) if isinstance(v, Decimal) else v for k, v in params.items()}
        return sql, params

    def snapshots(self, tenant, datasets, conn=None):
        return {d["id"]: self.catalog.rows(tenant, d, conn, limit=50001) for d in datasets}

    def eligible_rows(self, tenant, tree, bindings, dataset, rows):
        if len(bindings) != 1 or not isinstance(tree, (exp.Select, exp.Update, exp.Delete)):
            return rows
        if tree.find(exp.Join) or tree.find(exp.Subquery) or tree.find(exp.Exists):
            return rows
        where = tree.args.get("where")
        if not where:
            return rows

        def conjuncts(node):
            if isinstance(node, exp.Paren):
                return conjuncts(node.this)
            if isinstance(node, exp.And):
                return conjuncts(node.this) + conjuncts(node.expression)
            return [node]

        local_aliases = {
            node.alias_or_name for node in tree.find_all(exp.Table) if id(node) in bindings
        }
        filters = []
        stable_functions = {
            "AND",
            "OR",
            "NOT",
            "LOWER",
            "UPPER",
            "LENGTH",
            "COALESCE",
            "NULLIF",
            "ABS",
            "ROUND",
            "CAST",
        }
        for clause in conjuncts(where.this):
            if any(
                column.table and column.table not in local_aliases
                for column in clause.find_all(exp.Column)
            ):
                continue
            if clause.find(exp.Subquery) or clause.find(exp.AggFunc) or clause.find(exp.Window):
                continue
            functions = [
                (node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper())
                for node in clause.find_all(exp.Func)
            ]
            if any(name not in stable_functions for name in functions):
                continue
            filters.append(clause.copy())
        if not filters:
            return rows
        original = next((node for node in tree.find_all(exp.Table) if id(node) in bindings), None)
        if original is None:
            return rows
        table = original.copy()
        selection = exp.select(
            *[col(key, table.alias_or_name) for key in dataset["primary_key"]]
        ).from_(table)
        selection.set("where", exp.Where(this=exp.and_(*filters)))
        compiled, parameters = self.bind(selection, {id(table): dataset})
        with self.db.transaction(tenant) as connection:
            self.configure_transaction(connection)
            keys = {
                digest(serial(list(row))) for row in connection.execute(text(compiled), parameters)
            }
        return [
            row
            for row in rows
            if digest(serial([row[key] for key in dataset["primary_key"]])) in keys
        ]

    def lower_semantics(
        self, tenant, tree, bindings, snapshots, budget, accept, reject, progress=None
    ):
        from .semantic_types import SemanticSpec
        from .features import FeatureRegistry

        details, total = [], Counter()
        scopes = list(traverse_scope(tree))
        operators = []
        grouped = {}
        for function in list(tree.find_all(exp.Anonymous)):
            name = function.name.upper()
            if name not in ("SEMANTIC", "SEMANTIC_FEATURE"):
                continue
            if not self.semantics:
                raise ValueError("Jev is not configured")
            if (
                len(function.expressions) != 2
                or not isinstance(function.expressions[0], exp.Column)
                or not isinstance(function.expressions[1], exp.Literal)
                or not function.expressions[1].is_string
            ):
                raise ValueError(
                    name + " requires a source column and a literal definition or feature reference"
                )
            column, definition = function.expressions
            dataset, alias = None, column.table
            parent = function.find_ancestor(exp.Select)
            if parent:
                scope = next((scope for scope in scopes if scope.expression is parent), None)
                if scope:
                    source = scope.sources.get(alias)
                    if isinstance(source, exp.Table):
                        dataset = bindings.get(id(source))
            else:
                candidates = list(bindings.values())
                dataset = candidates[0] if len(candidates) == 1 else None
                alias = alias or (dataset["name"] if dataset else None)
            if not dataset:
                raise ValueError(
                    "Semantic predicates must bind to an unambiguous base-table column"
                )
            if column.name not in {item["name"] for item in dataset["columns"]}:
                raise ValueError("Unknown semantic column")
            if name == "SEMANTIC_FEATURE":
                registry = FeatureRegistry(self.db)
                feature = registry.resolve(tenant, dataset["id"], definition.this)
                spec = registry.spec(feature)
                if spec.column != column.name:
                    raise ValueError("Feature source column does not match its reviewed definition")
            else:
                spec = SemanticSpec(column.name, definition.this)
            group = grouped.setdefault(
                dataset["id"], {"dataset": dataset, "specs": {}, "scopes": {}}
            )
            group["specs"][spec.key] = spec
            local_tree = parent if parent is not None else tree
            group["scopes"][id(local_tree)] = local_tree
            operators.append((function, dataset, alias, spec))
        outcomes = {}
        for identity, group in grouped.items():
            dataset = group["dataset"]
            population = snapshots[identity]
            eligible_keys = set()
            for local_tree in group["scopes"].values():
                local_bindings = {
                    id(node): bindings[id(node)]
                    for node in local_tree.find_all(exp.Table)
                    if id(node) in bindings
                }
                branch = self.eligible_rows(tenant, local_tree, local_bindings, dataset, population)
                eligible_keys.update(
                    digest(serial([row[key] for key in dataset["primary_key"]])) for row in branch
                )
            eligible = [
                row
                for row in population
                if digest(serial([row[key] for key in dataset["primary_key"]])) in eligible_keys
            ]
            values, stats, metrics = self.semantics.ensure_many(
                tenant,
                dataset,
                eligible,
                list(group["specs"].values()),
                budget,
                accept,
                reject,
                should_continue=progress,
            )
            outcomes[identity] = (eligible, values)
            metrics["structured_rows_skipped"] = len(population) - len(eligible)
            for key, value in metrics.items():
                if key in ("concurrency", "peak_pending_requests"):
                    total[key] = max(total[key], value)
                else:
                    total[key] += value
            for spec in group["specs"].values():
                details.append(
                    {
                        "dataset_id": identity,
                        "column": spec.column,
                        "definition": spec.definition,
                        "kind": spec.kind,
                        "feature_id": spec.feature_id,
                        "context_columns": spec.context_columns,
                        **stats[spec.key],
                    }
                )
        for function, dataset, alias, spec in operators:
            eligible, resolved = outcomes[dataset["id"]]
            keys_by_value = {}
            for row in eligible:
                value = resolved[spec.key][
                    digest(serial([row[key] for key in dataset["primary_key"]]))
                ]
                if value is not None:
                    keys_by_value.setdefault(value, []).append(
                        [row[key] for key in dataset["primary_key"]]
                    )
            cases = []
            for value, keys in keys_by_value.items():
                left = (
                    col(dataset["primary_key"][0], alias)
                    if len(dataset["primary_key"]) == 1
                    else exp.Tuple(expressions=[col(key, alias) for key in dataset["primary_key"]])
                )
                literals = [
                    literal(key[0])
                    if len(key) == 1
                    else exp.Tuple(expressions=[literal(part) for part in key])
                    for key in keys
                ]
                cases.append(
                    exp.If(this=exp.In(this=left, expressions=literals), true=literal(value))
                )
            replacement = exp.Case(ifs=cases, default=exp.Null()) if cases else exp.Null()
            # PostgreSQL requires identical parameters in SELECT and GROUP BY expressions.
            replacement.meta["semantic_binding"] = (dataset["id"], alias, spec.key)
            function.replace(replacement)
        return tree, dict(total), details

    def configure_transaction(self, connection):
        if self.db.engine.dialect.name == "postgresql":
            connection.execute(text("SET LOCAL statement_timeout = '10000ms'"))
            connection.execute(text("SET LOCAL lock_timeout = '3000ms'"))

    def execute(
        self,
        tenant,
        sql,
        *,
        request="",
        plan=None,
        max_evaluations=100,
        accept=0.8,
        reject=0.2,
        allow_all=False,
        max_affected=1000,
        actor="local",
        mutation_token=None,
        expected_semantic_snapshot=None,
        progress=None,
    ):
        if not 0 <= max_evaluations <= 1000 or not 1 <= max_affected <= 1000:
            raise ValueError("Invalid execution budget")
        started = time.perf_counter()
        tree, bindings, datasets, target = self.prepare(tenant, sql)
        native_read = target is None and not any(
            node.name.upper() in ("SEMANTIC", "SEMANTIC_FEATURE")
            for node in tree.find_all(exp.Anonymous)
        )
        initial = {} if native_read else self.snapshots(tenant, datasets)
        if sum(len(v) for v in initial.values()) > 50000:
            raise ValueError(
                "Semantic queries and mutation previews support up to 50,000 source rows; use a smaller registered dataset"
            )
        source_hash = None if native_read else digest(serial(initial))
        budget = [max_evaluations]
        # Lower in place to preserve SQLGlot source identities used in lexical bindings.
        lowered, coverage, evidence = self.lower_semantics(
            tenant, tree, bindings, initial, budget, accept, reject, progress=progress
        )
        semantic_snapshot = digest(
            sorted(
                [
                    [
                        item["dataset_id"],
                        item["column"],
                        item["definition"],
                        item.get("feature_id") or "",
                        sorted(
                            [
                                [
                                    observation["row_key"],
                                    observation["decision"],
                                    observation.get("review_id", ""),
                                ]
                                for observation in item["observations"]
                            ],
                            key=lambda entry: entry[0],
                        ),
                    ]
                    for item in evidence
                ],
                key=lambda entry: entry[:4],
            )
        )
        if (
            expected_semantic_snapshot is not None
            and semantic_snapshot != expected_semantic_snapshot
        ):
            raise ValueError("Semantic evidence or review changed; inspect a new mutation preview")
        complete = not coverage.get("unknown", 0)
        if not complete and (
            tree.find(exp.Subquery)
            or tree.find(exp.Exists)
            or any(j.side in ("LEFT", "RIGHT", "FULL") for j in tree.find_all(exp.Join))
        ):
            raise ValueError(
                (
                    "Unresolved semantic decisions in subqueries or outer joins could change "
                    "absence/group membership; resolve them before execution"
                )
            )
        compiled, params = self.bind(lowered, bindings)
        is_write = target is not None
        if is_write and not complete:
            raise ValueError("Mutation cannot proceed while semantic membership is unresolved")
        if (
            is_write
            and isinstance(tree, (exp.Update, exp.Delete))
            and tree.args.get("where") is None
            and not allow_all
        ):
            raise ValueError(
                "A full-table mutation requires allow_all=true and an explicit preview"
            )
        isolation = (
            "REPEATABLE READ"
            if self.db.engine.dialect.name == "postgresql" and not mutation_token
            else None
        )
        with self.db.transaction(tenant, isolation_level=isolation) as connection:
            self.configure_transaction(connection)
            if mutation_token and self.db.engine.dialect.name == "postgresql":
                table = self.catalog.table(target, connection)
                q = connection.dialect.identifier_preparer.format_table(table)
                connection.execute(text(f"LOCK TABLE {q} IN SHARE ROW EXCLUSIVE MODE"))
            if native_read:
                if self.db.engine.dialect.name == "postgresql":
                    source_hash = connection.execute(
                        text("SELECT pg_current_snapshot()::text")
                    ).scalar_one()
                    snapshot_mode = "postgres_repeatable_read"
                else:
                    # SQLite's legacy driver does not begin transactions for SELECT.
                    connection.exec_driver_sql("BEGIN")
                    source_hash = "transaction:" + uid()
                    snapshot_mode = "sqlite_transaction"
                source_rows = 0
                for dataset in datasets:
                    table = self.catalog.table(dataset, connection)
                    quoted = connection.dialect.identifier_preparer.format_table(table)
                    source_rows += connection.execute(
                        text("SELECT COUNT(*) FROM " + quoted)
                    ).scalar_one()
            else:
                current = self.snapshots(tenant, datasets, connection)
                if digest(serial(current)) != source_hash:
                    raise ValueError("Source data changed during evaluation; rerun the query")
                source_rows = sum(len(v) for v in initial.values())
            manifest = {
                "dataset_ids": [d["id"] for d in datasets],
                "source_snapshot": source_hash,
                "semantic_snapshot": semantic_snapshot,
                "source_rows": source_rows,
                "snapshot_mode": snapshot_mode if native_read else "content_hash",
                "complete": complete,
                "semantic_coverage": coverage,
                "evaluator": self.decisions.model if self.decisions else None,
                "decision_policy": {"accept": accept, "reject": reject},
                "result_is_partial": not complete,
                "evidence": evidence,
                "operation": type(tree).__name__.lower(),
                "planning_ms": (plan or {}).get("planning_ms", 0),
            }
            if not is_write:
                bounded = "SELECT * FROM (" + compiled + ") AS _sdd_result LIMIT 1001"
                result = connection.execute(text(bounded), params)
                if len(result.keys()) != len(set(result.keys())):
                    raise ValueError("Duplicate output names require distinct SQL aliases")
                rows = [serial(dict(row)) for row in result.mappings()]
                manifest["truncated"] = len(rows) > 1000
                rows = rows[:1000]
                manifest["execution_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return self.save(
                    connection, tenant, request, sql, compiled, params, plan, manifest, rows
                )
            before = []
            sample = []
            if isinstance(tree, (exp.Update, exp.Delete)):
                target_node = tree.this.copy()
                selection = exp.select("*").from_(target_node)
                if tree.args.get("where"):
                    selection.set("where", tree.args["where"].copy())
                select_sql, select_params = self.bind(selection, {id(target_node): target})
                before = [
                    dict(r) for r in connection.execute(text(select_sql), select_params).mappings()
                ]
                affected = len(before)
                if isinstance(tree, exp.Update):
                    projected = exp.select(
                        *[
                            exp.alias_(a.expression.copy(), a.this.name, quoted=True)
                            for a in tree.expressions
                        ]
                    ).from_(target_node.copy())
                    if tree.args.get("where"):
                        projected.set("where", tree.args["where"].copy())
                    psql, pparams = self.bind(projected, {id(target_node): target})
                    sample = [
                        serial(dict(r)) for r in connection.execute(text(psql), pparams).mappings()
                    ][:20]
            else:
                affected = len(tree.expression.expressions)
                sample = [
                    {
                        c.name: v.sql(dialect="postgres")
                        for c, v in zip(tree.this.expressions, row.expressions)
                    }
                    for row in tree.expression.expressions[:20]
                ]
            if affected > max_affected:
                raise ValueError(
                    f"Mutation affects {affected} rows, exceeding the {max_affected}-row budget"
                )
            if not mutation_token:
                token = uid()
                connection.execute(
                    insert(schema.previews).values(
                        id=token,
                        tenant=tenant,
                        logical_sql=sql,
                        dataset_ids=manifest["dataset_ids"],
                        snapshot_hash=source_hash,
                        affected_rows=affected,
                        options={
                            "accept": accept,
                            "reject": reject,
                            "allow_all": allow_all,
                            "max_affected": max_affected,
                            "expected_semantic_snapshot": semantic_snapshot,
                        },
                        expires_at=time.time() + 600,
                        state="pending",
                        actor=actor,
                        created_at=now(),
                    )
                )
                manifest["execution_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return {
                    "mutation_preview": True,
                    "preview_token": token,
                    "expires_in_seconds": 600,
                    "logical_sql": sql,
                    "compiled_sql": compiled,
                    "parameters": serial(params),
                    "affected_rows": affected,
                    "before_sample": serial(before[:20]),
                    "changes_sample": sample,
                    "manifest": manifest,
                    "plan": plan or {},
                }
            preview = (
                connection.execute(
                    select(schema.previews)
                    .where(
                        schema.previews.c.id == mutation_token, schema.previews.c.tenant == tenant
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not preview
                or preview["state"] != "pending"
                or preview["expires_at"] < time.time()
                or preview["actor"] != actor
            ):
                raise ValueError(
                    "Preview is invalid, expired, used, or belongs to another reviewer"
                )
            if (
                preview["logical_sql"] != sql
                or preview["snapshot_hash"] != source_hash
                or preview["affected_rows"] != affected
            ):
                raise ValueError("Mutation preview is stale; inspect a new preview")
            # Lock the target table against concurrent inserts/updates between preview recheck and commit.
            if self.db.engine.dialect.name == "postgresql":
                table = self.catalog.table(target, connection)
                q = connection.dialect.identifier_preparer.format_table(table)
                connection.execute(text(f"LOCK TABLE {q} IN SHARE ROW EXCLUSIVE MODE"))
                if digest(serial(self.snapshots(tenant, datasets, connection))) != source_hash:
                    raise ValueError("Source changed; inspect a new preview")
            result = connection.execute(text(compiled), params)
            changed = result.rowcount
            connection.execute(
                update(schema.previews)
                .where(schema.previews.c.id == mutation_token, schema.previews.c.tenant == tenant)
                .values(state="committed")
            )
            if isinstance(tree, exp.Delete):
                row_keys = [digest(serial([r[p] for p in target["primary_key"]])) for r in before]
                connection.execute(
                    delete(schema.evidence).where(
                        schema.evidence.c.tenant == tenant,
                        schema.evidence.c.dataset_id == target["id"],
                        schema.evidence.c.row_key.in_(row_keys),
                    )
                )
                connection.execute(
                    delete(schema.row_versions).where(
                        schema.row_versions.c.tenant == tenant,
                        schema.row_versions.c.dataset_id == target["id"],
                        schema.row_versions.c.row_key.in_(row_keys),
                    )
                )
                connection.execute(
                    delete(schema.inference_calls).where(
                        schema.inference_calls.c.tenant == tenant,
                        schema.inference_calls.c.dataset_id == target["id"],
                        schema.inference_calls.c.row_key.in_(row_keys),
                    )
                )
                from .history import QueryHistory

                QueryHistory.redact_dataset(connection, tenant, target["id"])
                # Results may contain source text: redact retained runs over this deleted dataset.
                for old in connection.execute(
                    select(schema.runs).where(schema.runs.c.tenant == tenant)
                ).mappings():
                    if target["id"] in old["manifest"].get("dataset_ids", []):
                        connection.execute(
                            update(schema.runs)
                            .where(schema.runs.c.id == old["id"], schema.runs.c.tenant == tenant)
                            .values(
                                plan={},
                                parameters={},
                                result=[],
                                manifest={"status": "redacted_source_deleted"},
                            )
                        )
            from .features import FeatureRegistry

            FeatureRegistry(self.db).enqueue(tenant, target["id"], connection)
            manifest.update(
                committed=True,
                affected_rows=changed,
                execution_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return self.save(
                connection,
                tenant,
                request,
                sql,
                compiled,
                params,
                plan,
                manifest,
                [{"affected_rows": changed}],
            )

    def save(self, connection, tenant, request, sql, compiled, params, plan, manifest, rows):
        identity = uid()
        connection.execute(
            insert(schema.runs).values(
                id=identity,
                tenant=tenant,
                request=request,
                logical_sql=sql,
                compiled_sql=compiled,
                parameters=serial(params),
                plan=serial(plan or {}),
                manifest=serial(manifest),
                result=rows,
                created_at=now(),
            )
        )
        return {
            "run_id": identity,
            "logical_sql": sql,
            "compiled_sql": compiled,
            "parameters": serial(params),
            "plan": plan or {},
            "result": rows,
            "manifest": manifest,
        }

    def commit(self, tenant, token, actor):
        preview = self.catalog.ledger.get(tenant, schema.previews, token)
        return self.execute(
            tenant,
            preview["logical_sql"],
            actor=actor,
            mutation_token=token,
            max_evaluations=0,
            **preview["options"],
        )
