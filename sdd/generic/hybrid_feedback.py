"""Observed values, backend checks and concrete feedback for one bounded repair."""

from copy import deepcopy

from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.lineage import lineage
from sqlglot.errors import SqlglotError

from .catalog import serial
from .sql import FUNCTIONS


def backend_contract(catalog):
    sqlite = catalog.db.engine.dialect.name == "sqlite"
    return {
        "input_dialect": "PostgreSQL, restricted to the registered calculation catalog",
        "execution_backend": catalog.db.engine.dialect.name,
        "allowed_functions": sorted(FUNCTIONS),
        "construction_rules": [
            "Use only listed functions. AGE and regular-expression predicates are not registered.",
            "Use exact SQL comparisons for codes, named categories, dates and quantities. SEMANTIC is for meanings that cannot be represented by known values and explicit predicates.",
            "SEMANTIC requires a base text column and a registered nonempty primary key. Never use it to compensate for not inspecting a category's stored representation.",
            "A value sample is not a closed vocabulary or a population filter. Do not infer absence from an omitted sample value.",
            *(
                [
                    "SQLite cannot execute LATERAL joins. Prefer windows or scalar correlated subqueries.",
                    "Prefer EXTRACT for ISO dates. Do not use PostgreSQL date/interval arithmetic or formatted TO_TIMESTAMP without an executable alternative.",
                ]
                if sqlite
                else []
            ),
        ],
    }


def observed_context(tenant, packet, catalog):
    """Read bounded records only after retrieval; samples never redefine query scope."""
    output = deepcopy(packet)
    output["backend_contract"] = backend_contract(catalog)
    trace = {
        "output_state": "VALUE",
        "operation_state": "SUCCEEDED",
        "tables": 0,
        "sample_only": True,
    }
    datasets = {d["name"]: d for d in catalog.list(tenant)}
    for item in output["catalog"][:16]:
        dataset = datasets[item["name"]]
        columns = [c for c in item["columns"] if not c.get("feature_id")][:24]
        try:
            with catalog.db.transaction(tenant) as connection:
                if catalog.db.engine.dialect.name == "postgresql":
                    connection.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                table = catalog.table(dataset, connection)
                fields = [
                    func.substr(table.c[c["name"]], 1, 160).label(c["name"])
                    if c["type"] == "text"
                    else table.c[c["name"]]
                    for c in columns
                    if c["name"] in table.c
                ]
                if not fields:
                    continue
                rows = connection.execute(select(*fields).limit(256)).mappings().all()
            for column in columns:
                values = []
                for row in rows:
                    value = serial(row.get(column["name"]))
                    if value is not None and value not in values and len(str(value)) <= 160:
                        values.append(value)
                    if len(values) >= 16:
                        break
                column["value_evidence"] = {
                    "examples": values,
                    "sample_only": True,
                    "rows_inspected": len(rows),
                    "observed_nulls": sum(row.get(column["name"]) is None for row in rows),
                    "observed_blanks": sum(
                        isinstance(row.get(column["name"]), str) and not row[column["name"]].strip()
                        for row in rows
                    ),
                    "text_may_be_truncated": column["type"] == "text",
                }
            trace["tables"] += 1
        except (ValueError, DBAPIError) as exc:
            trace.setdefault("unavailable", []).append(
                {"table": item["name"], "operation_state": "FAILED", "code": type(exc).__name__}
            )
    if trace.get("unavailable"):
        trace.update(output_state="UNKNOWN", operation_state="FAILED")
    return output, trace


def checked_candidate(service, tenant, candidate, allowed):
    item = dict(candidate)
    try:
        tree, bindings, _, mutation = service.prepare(tenant, item["sql"])
        if any(d["id"] not in allowed for d in bindings.values()):
            raise ValueError("Query references an unselected dataset")
        semantic = any(
            n.name.upper() in ("SEMANTIC", "SEMANTIC_FEATURE") for n in tree.find_all(exp.Anonymous)
        )
        unkeyed_semantic = any(
            not bindings[id(source)]["primary_key"]
            for scope in traverse_scope(tree)
            for node in scope.find_all(exp.Anonymous)
            if node.name.upper() in ("SEMANTIC", "SEMANTIC_FEATURE")
            and node.expressions
            and isinstance(node.expressions[0], exp.Column)
            for _, source in [scope.selected_sources.get(node.expressions[0].table, (None, None))]
            if isinstance(source, exp.Table) and id(source) in bindings
        )
        if unkeyed_semantic:
            raise ValueError(
                "Semantic row evaluation requires registered primary keys; use observed category values and ordinary SQL when the condition is categorical"
            )
        item.update(valid=True, operation=tree.key if mutation is not None else "select")
        if mutation is not None or semantic:
            item["backend_probe"] = {"output_state": "NOT_EVALUATED", "operation_state": "SKIPPED"}
        else:
            compiled, parameters = service.bind(tree, bindings)
            with service.db.transaction(tenant) as connection:
                service.configure_transaction(connection)
                prefix = (
                    "EXPLAIN QUERY PLAN "
                    if service.db.engine.dialect.name == "sqlite"
                    else "EXPLAIN (FORMAT JSON) "
                )
                connection.execute(text(prefix + compiled), parameters).all()
            item["backend_probe"] = {"output_state": "VALUE", "operation_state": "SUCCEEDED"}
        item["projections"] = (
            [p.sql(dialect="postgres") for p in tree.selects] if mutation is None else []
        )
    except (ValueError, SqlglotError, DBAPIError) as exc:
        item.update(
            valid=False,
            error=str(exc).split("[SQL:")[0][:400],
            backend_probe={"output_state": "NOT_EVALUATED", "operation_state": "FAILED"},
        )
    return item


def repair_feedback(plan):
    trace = plan["hybrid"]
    candidates = trace.get("candidates", [])
    selected = next((c for c in candidates if c["id"] == trace.get("selected")), None)
    if selected is None:
        return [
            {
                "kind": "executable_sql",
                "candidate": c["id"],
                "problem": c.get("error", "No executable selection"),
            }
            for c in candidates
        ]
    # A probability alone is not an actionable editing instruction.
    feedback = list(trace.get("repair_tickets", []))
    if feedback:
        feedback.append(
            {
                "kind": "additional_context",
                "items": trace.get("plan_inspection", {})
                .get("additional_data", {})
                .get("helpful", []),
            }
        )
    return feedback


def quality(plan):
    trace = plan["hybrid"]
    checks = [
        v
        for k, v in trace.get("checks", {}).items()
        if k.startswith("check_" + trace.get("selected", "!") + "_")
    ]
    return (
        bool(plan.get("logical_sql")),
        min(checks, default=0),
        (trace.get("overall_appropriateness", {}).get("probability") or 0),
    )


def output_review_jobs(packet, candidates, noul, choice):
    """Review answer fields in parallel with SQL checks, without plan self-justification."""
    jobs = []
    for candidate in candidates:
        if not candidate.get("projections"):
            continue
        expressions = {}
        try:
            nodes = lineage(None, parse_one(candidate["sql"], read="postgres"), dialect="postgres")
            expressions = {
                name: [node.expression.sql(dialect="postgres") for node in root.walk()][:24]
                for name, root in nodes.items()
            }
        except (SqlglotError, ValueError):
            pass
        jobs.append(
            (
                {
                    "request": packet["request"],
                    "output_expressions": dict(enumerate(candidate["projections"])),
                    "expression_definitions": expressions,
                    "candidates": [
                        {"id": candidate["id"], "projections": candidate["projections"]}
                    ],
                },
                {
                    **{
                        f"check_role_{candidate['id']}_output{i}": choice(
                            f"What role does output expression {i} play in the ORIGINAL request? Classify the requested answer, not whether this field is useful to SQL. A ranking measure is not implicitly requested by a who/which-entity question.",
                            {
                                "answer": "Requested answer field or grouping identifier",
                                "support": "Only an intermediate filter, ranking or explanatory helper",
                                "ambiguous": "Cannot determine the requested answer shape",
                            },
                        )
                        for i in range(len(candidate["projections"]))
                    },
                    **{
                        f"check_{candidate['id']}_output{i}": noul(
                            f"Is output expression {i} itself requested as an answer, rather than only used to identify, filter or rank the answer? "
                            "Infer the answer target from the original request. Explanatory metrics and supporting dates are not requested unless the user asks for them. "
                            "Preserve requested identifiers, measures and grouping labels."
                        )
                        for i in range(len(candidate["projections"]))
                    },
                },
            )
        )
    return jobs


def review_context(packet):
    """Keep structural evidence and short value examples; avoid repeated profiling payloads."""
    output = deepcopy(packet)
    output.pop("backend_contract", None)
    for table in output.get("catalog", []):
        for column in table["columns"]:
            evidence = column.pop("value_evidence", None)
            if evidence:
                column["sample_values"] = evidence.get("examples", [])[:4]
                column["sample_missingness"] = {
                    k: evidence[k]
                    for k in ("rows_inspected", "observed_nulls", "observed_blanks")
                    if k in evidence
                }
    return output


def projection_repair(feedback):
    relevant = [f for f in feedback if f["kind"] != "additional_context"]
    return (
        bool(relevant)
        and all(
            f["kind"] == "output" or (f["kind"].startswith("output") and f["kind"][6:].isdigit())
            for f in relevant
        )
        and not any(f.get("items") for f in feedback)
    )


def preserves_repair_scope(original, revised):
    """A projection-only repair cannot change relational clauses or shared CTEs."""
    try:
        trees = [parse_one(sql, read="postgres") for sql in (original, revised)]
        if not all(isinstance(tree, exp.Select) for tree in trees):
            return original == revised
        for tree in trees:
            tree.set("expressions", [])
            for node in tree.walk():
                node.comments = None
        return trees[0] == trees[1]
    except SqlglotError:
        return False
