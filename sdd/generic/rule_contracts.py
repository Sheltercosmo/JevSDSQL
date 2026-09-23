"""Bounded semantic contracts for relational business rules and observation scope."""

from .jev import choice
from .stage_dag import operation as op, ref, value


def operands(graph):
    terms = {key: graph.bindings.get(key, ref(key)) for key in graph.fields}
    labels = {key: field.label for key, field in graph.fields.items()}
    for identity in graph.terms:
        terms["k" + identity] = ref("k" + identity)
        labels["k" + identity] = graph.rules[identity].name
    return terms, labels


def resolve_relational_rules(planner, tenant, graph, roots):
    unresolved = [key for key in roots if key in graph.errors]
    if not unresolved:
        return
    states = {
        key: {"rule": graph.rules[key].name, "definition": graph.rules[key].definition}
        for key in unresolved
    }
    jobs = [
        (
            states[key],
            {
                "kb_relation_kind_" + key: choice(
                    "What operation does this rule define? Select only a representable operation; do not discard an unsupported part of the definition.",
                    {
                        "unsupported": "Another operation or unresolved definition",
                        "predicate": "Boolean eligibility conditions on values or named entity totals",
                        "quartile": "Four value-preserving quantile groups across the full population",
                        "percent_rank": "Percentile rank across the population",
                        "avg": "Mean within a population while retaining individual rows",
                    },
                )
            },
        )
        for key in unresolved
    ]
    kinds = planner.batch(tenant, jobs, "independent_relational_rules")
    terms, labels = operands(graph)
    for key in unresolved:
        kind = kinds["kb_relation_kind_" + key]
        if kind == "unsupported":
            continue
        state = states[key]
        try:
            if kind == "predicate":
                term = predicate_contract(planner, tenant, state, key, terms, labels, graph.columns)
            else:
                numeric = {
                    k: label
                    for k, label in labels.items()
                    if terms[k].type(graph.columns).kind in {"number", "integer"}
                    and terms[k].type(graph.columns).unit != "identifier"
                }
                answer = planner.ask(
                    tenant,
                    state,
                    {
                        "kb_window_operand_" + key: choice(
                            "Value whose distribution this rule measures", numeric
                        ),
                        "kb_window_partition_" + key: choice(
                            "Separate distributions per which attribute? Use one overall population unless groups are explicitly required.",
                            {"none": "One overall population", **labels},
                        ),
                    },
                    "relational_rule_operands",
                )
                operand = answer["kb_window_operand_" + key]
                partition = answer["kb_window_partition_" + key]
                term = graph.register_window(
                    kind, terms[operand], [] if partition == "none" else [terms[partition]]
                )
            graph.bind(key, term)
            terms["k" + key], labels["k" + key] = ref("k" + key), graph.rules[key].name
        except (ValueError, KeyError) as exc:
            graph.errors[key] = str(exc)


def predicate_contract(planner, tenant, state, identity, terms, labels, columns):
    from .planner import literals

    constants = literals(state["definition"])
    values = list(dict.fromkeys([*constants["numbers"], *constants["strings"]]))[:32]
    if not values:
        raise ValueError("Predicate has no supported explicit constants")
    choices = {str(i): repr(item) for i, item in enumerate(values)}
    questions = {
        "kb_boolean_" + identity: choice(
            "How are the rule's conditions combined?",
            {
                "and": "Every condition is required",
                "or": "Any condition suffices",
                "unsupported": "Mixed Boolean nesting or another unsupported relationship",
            },
        )
    }
    for slot in range(4):
        prefix = f"kb_clause_{identity}_{slot}"
        questions[prefix + "_operand"] = choice(
            f"Operand of condition {slot + 1}, in the definition's order. Select none when there is no further condition. Named totals must use their compiled rule rather than a similarly named stored counter.",
            {"none": "No additional condition", **labels},
        )
        questions[prefix + "_operator"] = choice(
            f"Comparison in condition {slot + 1}. Preserve strict versus inclusive boundaries.",
            {
                "gt": "Greater than",
                "ge": "At least",
                "lt": "Less than",
                "le": "At most",
                "eq": "Equals",
                "ne": "Does not equal",
            },
        )
        questions[prefix + "_value"] = choice(f"Explicit constant in condition {slot + 1}", choices)
    answer = planner.ask(tenant, state, questions, "independent_population_predicates")
    connective = answer["kb_boolean_" + identity]
    if connective == "unsupported":
        raise ValueError("Mixed population predicates require an explicit structured definition")
    clauses = []
    for slot in range(4):
        prefix = f"kb_clause_{identity}_{slot}"
        key = answer[prefix + "_operand"]
        if key == "none":
            continue
        operand = terms[key]
        constant = values[int(answer[prefix + "_value"])]
        if isinstance(constant, (float, int)) and operand.type(columns).kind not in {
            "number",
            "integer",
        }:
            raise ValueError("Numeric predicate must bind a numeric operand")
        term = op(answer[prefix + "_operator"], operand, value(constant))
        if term not in clauses:
            clauses.append(term)
    if not clauses:
        raise ValueError("No population conditions could be bound")
    result = clauses[0]
    for term in clauses[1:]:
        result = op(connective, result, term)
    return result


def latest_contract(planner, tenant, request, table, datasets, fields, columns):
    if table == "none":
        return None
    dataset = next(d for d in datasets if d["name"] == table)
    local = {key: field for key, field in fields.items() if field.table == table}
    partitions = {
        key: field.label
        for key, field in local.items()
        if field.name in {link["source_column"] for link in dataset["links"]}
    }
    if not partitions:
        partitions = {key: field.label for key, field in local.items()}
    answer = planner.ask(
        tenant,
        {"request": request, "observation_table": table},
        {
            "kb_latest_partition": choice(
                "Which entity reference groups observations of the SAME entity?", partitions
            ),
            "kb_latest_order": choice(
                "Chronology for the latest observation. Prefer the actual observation timestamp. An expiry/deadline is not event time; when event time is absent, use a documented increasing observation sequence.",
                {key: field.label for key, field in local.items()},
            ),
        },
        "latest_observation_binding",
    )
    partition = [local[answer["kb_latest_partition"]].name]
    for link in dataset["links"]:
        if link["source_column"] == partition[0] and link.get("constraint_id"):
            partition = [
                item["source_column"]
                for item in dataset["links"]
                if item.get("constraint_id") == link["constraint_id"]
            ]
            break
    order = [(local[answer["kb_latest_order"]].name, True)]
    order.extend((key, True) for key in dataset["primary_key"] if key != order[0][0])
    return {
        "table": table,
        "columns": {field.name: columns[key] for key, field in local.items()},
        "partition": partition,
        "order": order,
    }


def window_scopes(planner, tenant, request, graph, restrictions):
    if not restrictions or not graph.windows:
        return {}
    jobs = []
    for key, (kind, operand, partition, _) in graph.windows.items():
        definitions = [
            r.definition
            for identity, r in graph.rules.items()
            if key in graph.terms.get(identity, value(None)).columns()
        ]
        state = {
            "request": request,
            "statistic": kind,
            "measure": operand.sql().sql(),
            "definition": definitions,
            "available_values": {k: v.label for k, v in graph.columns.items()},
            "restrictions": {str(i): term.sql().sql() for i, term in enumerate(restrictions)},
        }
        jobs.append(
            (
                state,
                {
                    f"kb_scope_{key}_{i}": choice(
                        f"Does restriction {i} define the comparison population for THIS statistic? Apply it before the window only when the requested comparison is within that restricted cohort. A rule defined over all entities retains the full population even if the final answer selects eligible entities. A restriction on this statistic itself must be applied after it.",
                        {
                            "after": "Calculate on the full input population; restrict the final answer afterward",
                            "before": "Restrict the comparison population before calculating this statistic",
                        },
                    )
                    for i in range(len(restrictions))
                },
            )
        )
    picks = planner.batch(tenant, jobs, "independent_window_scopes")
    return {
        key: tuple(i for i in range(len(restrictions)) if picks[f"kb_scope_{key}_{i}"] == "before")
        for key in graph.windows
    }


def entity_contract(planner, tenant, request, graph, datasets, initial):
    """Reconcile a lexical table guess with explicit per-entity rule bindings."""
    if not graph.reductions:
        return initial
    by_id = {d["id"]: d for d in datasets}
    anchors = []
    for _, _, _, bindings in graph.reductions.values():
        for left, right in bindings:
            for column, placeholder in ((left, right), (right, left)):
                if len(placeholder) != 1:
                    continue
                matches = {
                    by_id[link["target_id"]]["name"]
                    for d in datasets
                    for link in d["links"]
                    if link["target_id"] in by_id
                    and column.casefold()
                    in {
                        link["source_column"].casefold(),
                        (d["name"] + "." + link["source_column"]).casefold(),
                    }
                }
                if matches:
                    anchors.append(matches)
    compatible = set.intersection(*anchors) if anchors else {d["name"] for d in datasets}
    if not compatible:
        raise ValueError("Business rules bind incompatible entity populations")
    correction = planner.answers.get("kb_population", {}).get("_selection")
    if correction:
        if correction not in compatible:
            raise ValueError(
                "The corrected population conflicts with explicit rule entity bindings"
            )
        return correction
    answer = planner.ask(
        tenant,
        {
            "request": request,
            "rules": [
                {"name": graph.rules[k].name, "definition": graph.rules[k].definition}
                for k in graph.terms
            ],
            "initial_table_guess": initial,
        },
        {
            "kb_entity_population": choice(
                "Base entity whose individual records/totals contribute to the answer. For totals per account/device/person use that entity's table, not a child measurement table or the table storing a grouping label. The final report may group several base entities together.",
                {
                    d["name"]: d["description"] + "; entity key: " + ", ".join(d["primary_key"])
                    for d in datasets
                    if d["name"] in compatible
                },
            )
        },
        "entity_scope_reconciliation",
    )
    return answer["kb_entity_population"]
