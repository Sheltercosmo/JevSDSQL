"""Two independently filtered populations joined only after typed aggregation."""

from dataclasses import asdict
from itertools import combinations

from sqlglot import exp

from .jev import choice, noul
from .relational import Field, Link, Program
from .stage_dag import Column, StageDAG, operation as op, ref, value
from .value_evidence import value_relevance


OPERATIONS = {
    "ordinary": "Not a scalar comparison between two aggregate populations",
    "count_difference": "Number in the first population minus number in the second",
    "count_ratio": "Number in first population divided by number in second",
    "count_percentage": "100 times subset count divided by reference population count",
    "sum_difference": "Sum of a numeric measure in first population minus its sum in second",
    "sum_ratio": "Sum of a numeric measure in first population divided by its sum in second",
}


def predicate_options(key, kind, candidates):
    options = {
        "none": ("No restriction", None),
        "null": ("Missing / NULL", op("is_null", ref(key))),
        "not_null": ("Available / not NULL", op("not_null", ref(key))),
    }
    numeric = kind in {"number", "integer"}
    operators = {"eq": "equals", "ne": "does not equal"}
    if numeric:
        operators.update(lt="less than", le="at most", gt="greater than", ge="at least")
        options["zero_or_null"] = (
            "Zero OR missing",
            op("or", op("eq", ref(key), value(0)), op("is_null", ref(key))),
        )
    candidates = list(dict.fromkeys(candidates))
    for index, candidate in enumerate(candidates):
        for operation, label in operators.items():
            options[f"v{index}_{operation}"] = (
                label + " " + str(candidate),
                op(operation, ref(key), value(candidate)),
            )
    if numeric:
        for index, (low, high) in enumerate(combinations(sorted(candidates), 2)):
            options[f"range{index}"] = (
                f"Between {low} and {high} inclusive",
                op("and", op("ge", ref(key), value(low)), op("le", ref(key), value(high))),
            )
    return options


def cohort_dag(
    query, columns, left, right, *, aggregate="count", operand=None, operation="difference"
):
    """Shared source, sibling filters/reductions, then a scalar join and arithmetic."""
    dag = StageDAG()
    source = dag.source(query, columns)
    shared = [term for term in left if term in right]
    if shared:
        source = dag.filter(source, conjunction(shared), "Restrictions shared by both populations")
    nodes = []
    metric = op(aggregate, ref(operand)) if operand else op(aggregate)
    for name, filters in (("left", left), ("right", right)):
        filters = [term for term in filters if term not in shared]
        branch = (
            dag.filter(source, conjunction(filters), name + " population") if filters else source
        )
        nodes.append(
            dag.aggregate(
                branch, [], {name: metric}, "Reduce " + name + " population independently"
            )
        )
    combined = dag.join(
        nodes[0],
        nodes[1],
        [],
        outputs={"left": ref("l.left"), "right": ref("r.right")},
        how="cross",
    )
    expression = op("sub" if operation == "difference" else "div", ref("left"), ref("right"))
    if operation == "percentage":
        expression = op("mul", value(100), expression)
    final = dag.project(combined, {"result_1": expression})
    return dag, final


def conjunction(terms):
    if not terms:
        raise ValueError("An empty condition is not false")
    result = terms[0]
    for term in terms[1:]:
        result = op("and", result, term)
    return result


def try_cohort(planner, tenant, question, datasets):
    gate = planner.ask_factors(
        tenant,
        {"request": question},
        {
            "cohort_operation": choice(
                "For a READ calculation only, is the final answer a single difference, ratio or percentage of two independently restricted counts/sums? A difference of raw columns, a list of entities, a write request, or comparison to a mean is ordinary. SUBTRACT two counts is count_difference, never sum of identifier values.",
                OPERATIONS,
            ),
        },
        "cohort_intent",
    )["cohort_operation"]
    if gate == "ordinary":
        return None
    from .planner import literals

    datasets = planner.focus(tenant, question, datasets)
    fields = {}
    by_name = {d["name"]: d for d in datasets}
    by_id = {d["id"]: d for d in datasets}
    for dataset in datasets:
        for column in dataset["columns"]:
            key = f"f{len(fields)}"
            fields[key] = Field(
                key, dataset["name"], column["name"], column["type"], column.get("description", "")
            )
    links = [
        Link(
            d["name"],
            by_id[link["target_id"]]["name"],
            link["source_column"],
            link["target_column"],
            constraint_id=link.get("constraint_id"),
        )
        for d in datasets
        for link in d["links"]
        if link["target_id"] in by_id
    ]
    state = {
        "request": question,
        "comparison": OPERATIONS[gate],
        "fields": [asdict(f) for f in fields.values()],
    }
    roles = {
        "cohort_root": choice(
            "Which table contains the observations or entities being counted/measured?",
            {d["name"]: d["description"] or d["name"] for d in datasets},
        ),
        "cohort_identity": choice(
            "What is one counted unit? Use distinct entity identity when joins can repeat that same entity. Use stored observations for counts of tests/events. Do not sum identity numbers.",
            {
                "rows": "Individual source observations, including repeated entities",
                **{k: f.label for k, f in fields.items()},
            },
        ),
    }
    if gate.startswith("sum_"):
        roles["cohort_measure"] = choice(
            "Numeric measurement summed in each population, excluding identifiers.",
            {k: f.label for k, f in fields.items() if f.kind in {"integer", "number"}},
        )
    selected = planner.ask_factors(tenant, state, roles, "cohort_unit_contract")
    roles = {
        "cohort_filter_" + k: choice(
            f"Does {f.label} restrict either population? Include shared restrictions and conditions defining either numerator/first or denominator/second. Merely being counted is not a filter.",
            {"yes": "Restriction required", "no": "No restriction"},
        )
        for k, f in fields.items()
    }
    selected.update(
        planner.ask_factors(
            tenant,
            {"request": question, "comparison": OPERATIONS[gate]},
            roles,
            "cohort_field_contracts",
        )
    )
    numbers = [float(n) if n != int(n) else int(n) for n in literals(question)["numbers"]]
    options, questions = {}, {}
    for key, field in fields.items():
        if selected["cohort_filter_" + key] != "yes":
            continue
        candidates = (
            list(dict.fromkeys([*numbers, 0]))[:10]
            if field.kind in {"number", "integer"}
            else sorted(
                planner.domain(tenant, by_name[field.table], field.name),
                key=lambda v: (-value_relevance(question, v), str(v)),
            )[:40]
        )
        options[key] = predicate_options(key, field.kind, candidates)
        for branch in ("left", "right"):
            questions[f"cohort_{branch}_{key}"] = choice(
                f"Exact predicate on {field.label} for the {branch} population. Left is first quantity/numerator; right is second quantity/denominator. A shared restriction must appear in both. Do not reverse populations. Choose none for no restriction on this population. Use business definitions when supplied; keep comparisons exact.",
                {k: label for k, (label, _) in options[key].items()},
            )
    predicates = planner.ask_factors(
        tenant,
        {"request": question, "comparison": OPERATIONS[gate]},
        questions,
        "cohort_predicates",
    )
    branches = {}
    for branch in ("left", "right"):
        branches[branch] = [
            options[key][predicates[f"cohort_{branch}_{key}"]][1]
            for key in options
            if predicates[f"cohort_{branch}_{key}"] != "none"
        ]
    if branches["left"] == branches["right"] and gate.endswith(("ratio", "percentage")):
        scopes = planner.ask_factors(
            tenant,
            {"request": question},
            {
                "cohort_scope_" + key: choice(
                    "For "
                    + fields[key].label
                    + " "
                    + options[key][predicates[f"cohort_left_{key}"]][0]
                    + ", does this define the measured subset (left/numerator only), reference population (both), or denominator only?",
                    {
                        "left": "Numerator subset only",
                        "both": "Reference population shared by numerator and denominator",
                        "right": "Denominator only",
                    },
                )
                for key in options
                if predicates[f"cohort_left_{key}"] != "none"
            },
            "cohort_scope_reconciliation",
        )
        branches = {
            branch: [
                options[key][predicates[f"cohort_left_{key}"]][1]
                for key in options
                if predicates[f"cohort_left_{key}"] != "none"
                and scopes["cohort_scope_" + key] in (branch, "both")
            ]
            for branch in ("left", "right")
        }
    if branches["left"] == branches["right"]:
        raise ValueError("The two requested populations were not distinguished")
    identity = selected["cohort_identity"]
    aggregate, operand = ("count", None) if identity == "rows" else ("count_distinct", identity)
    if gate.startswith("sum_"):
        aggregate, operand = "sum", selected["cohort_measure"]
    needed = {k for terms in branches.values() for term in terms for k in term.columns()}
    if operand:
        needed.add(operand)
    root = selected["cohort_root"]
    if not needed:
        needed.add(next(k for k, f in fields.items() if f.table == root))
    from .relationship_roles import bind_roles

    links = bind_roles(
        lambda t, s, q: planner.ask_factors(t, s, q, "relationship_roles"),
        tenant,
        question,
        links,
        [fields[k] for k in needed],
    )
    query, aliases, _ = Program(root, fields, links, outputs=sorted(needed)).base()
    query = query.select(
        *[exp.alias_(fields[k].expression(aliases), k, quoted=True) for k in sorted(needed)]
    )
    identifiers = {(d["name"], k) for d in datasets for k in d["primary_key"]}
    columns = {
        k: Column(
            fields[k].kind,
            fields[k].label,
            unit="identifier" if (fields[k].table, fields[k].name) in identifiers else None,
        )
        for k in needed
    }
    dag, final = cohort_dag(
        query,
        columns,
        branches["left"],
        branches["right"],
        aggregate=aggregate,
        operand=operand,
        operation=gate.split("_", 1)[1],
    )
    sql = dag.compile(final).sql(dialect="postgres", pretty=True)
    audit = planner.ask_factors(
        tenant,
        {"request": question, "logical_sql": sql},
        {
            "complete": noul(
                "Does the complete proposal preserve the requested counted entity, both population scopes, exact boundaries, output and arithmetic? Conflicting definitions or ambiguous metric mean uncertain, not complete."
            )
        },
        "cohort_audit",
    )
    return {
        "operation": "select",
        "logical_sql": sql,
        "stage_dag": dag.describe(final),
        "evidence_graph": planner.graph,
        "_unresolved": []
        if audit["complete"] >= 0.8
        else [
            {
                "code": "cohort_contract",
                "detail": "Confirm the counted unit and both population restrictions before execution.",
            }
        ],
    }
