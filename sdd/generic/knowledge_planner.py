"""Parallel rule/field grounding followed by typed relational composition."""

from dataclasses import asdict
from collections import deque
import re

from sqlglot import exp

from .jev import choice, noul, selected
from .assignment import assign_unique
from .cohort_planner import predicate_options
from .knowledge import RuleGraph, rules_from
from .rule_contracts import (
    entity_contract,
    latest_contract,
    resolve_relational_rules,
    window_scopes,
)
from .rule_stages import compile_values, latest_source, validate_bindings
from .parallel_planner import chunks
from .relational import Field, Link, Program
from .planning_phases import placement
from .stage_dag import Column, StageDAG, operation as op, ref, value


AGGREGATES = {
    "value": "Return the value; if other outputs are aggregated, group by this attribute",
    "avg": "Mean of row values",
    "sum": "Sum of row values",
    "min": "Minimum",
    "max": "Maximum",
    "count": "Count rows, or rows satisfying the selected Boolean rule",
    "count_distinct": "Count distinct operand values",
    "share": "Percentage: this group's row count divided by all groups' count",
    "conditional_share": "Percentage of rows within this group satisfying the Boolean rule",
}


class KnowledgePlanner:
    def __init__(self, catalog, decisions):
        self.catalog, self.decisions = catalog, decisions
        self.rounds = []
        self.answers = {}

    def batch(self, tenant, jobs, phase):
        fixed, pending = {}, []
        for state, questions in jobs:
            remaining = {}
            for key, item in questions.items():
                if item["type"] == "choice" and len(item["criteria"]) == 1:
                    chosen = next(iter(item["criteria"]))
                    fixed[key] = {
                        "type": "choice",
                        "choice": chosen,
                        "probabilities": {chosen: 1.0},
                    }
                else:
                    remaining[key] = item
            if remaining:
                pending.append((state, remaining))
        jobs = pending
        self.rounds.append(
            {"phase": phase, "independent_batches": len(jobs), "placement": placement(phase)}
        )
        answers = {
            key: answer
            for result in self.decisions.ask_many(tenant, jobs)
            for key, answer in result["answers"].items()
        }
        answers.update(fixed)
        self.answers.update(answers)
        return {
            key: selected(answer) if answer["type"] == "choice" else answer["noul"]
            for key, answer in answers.items()
        }

    def ask(self, tenant, state, questions, phase):
        fixed = {
            key: next(iter(item["criteria"]))
            for key, item in questions.items()
            if item["type"] == "choice" and len(item["criteria"]) == 1
        }
        pending = [(key, item) for key, item in questions.items() if key not in fixed]
        return {
            **fixed,
            **self.batch(tenant, [(state, dict(page)) for page in chunks(pending, 24)], phase),
        }

    def plan(self, tenant, question, datasets, knowledge):
        from .planner import literals

        numbers = literals(question)["numbers"]
        rules = rules_from(knowledge)
        fields, columns = {}, {}
        for dataset in datasets:
            for column in dataset["columns"]:
                key = "f" + str(len(fields))
                fields[key] = Field(
                    key,
                    dataset["name"],
                    column["name"],
                    column["type"],
                    column.get("description", ""),
                )
                columns[key] = Column(
                    column["type"],
                    fields[key].label,
                    column.get("nullable", True),
                    "identifier" if column["name"] in dataset["primary_key"] else None,
                    (dataset["name"] + "." + column["name"],),
                )
        candidates, virtual = dict(fields), {}
        for dataset in datasets:
            for column in dataset["columns"]:
                base = next(
                    k
                    for k, f in fields.items()
                    if f.table == dataset["name"] and f.name == column["name"]
                )
                for path, metadata in list(column.get("json_fields", {}).items())[:64]:
                    if not isinstance(path, str) or len(path.split(".")) > 4:
                        raise ValueError("JSON field paths require at most four named components")
                    key = "j" + str(len(virtual))
                    term = ref(base)
                    for part in path.split("."):
                        term = op("json_text", term, value(part))
                    kind = metadata.get("type", "text")
                    if kind in {"number", "integer"}:
                        term = op("number", term)
                    virtual[key] = term
                    candidates[key] = Field(
                        key,
                        dataset["name"],
                        column["name"] + "." + path,
                        kind,
                        metadata.get("description", ""),
                    )
                    columns[key] = term.type(columns)
        jobs = []
        for page in chunks(candidates.items(), 32):
            state = {
                "request": question,
                "fields": {
                    key: {
                        "table": field.table,
                        "name": field.name,
                        "type": field.kind,
                        "meaning": field.description[:360],
                        "primary_key": columns[key].unit == "identifier",
                    }
                    for key, field in page
                },
            }
            jobs.append(
                (
                    state,
                    {
                        "kb_field_" + key: choice(
                            f"Is {field.table}.{field.name} directly requested as an output attribute or statistic operand? A field explicitly requested must remain an output even when also used inside a formula. Exclude only operands that are not themselves requested.",
                            {
                                "output": "Directly requested output",
                                "filter": "Required restriction on stored values",
                                "output_filter": "Both returned and restricted",
                                "unused": "Only an internal operand or irrelevant",
                            },
                        )
                        for key, field in page
                    },
                )
            )
        for page in chunks(rules.items(), 32):
            state = {
                "request": question,
                "business_rule_names": {
                    key: {"name": rule.name, "type": rule.kind} for key, rule in page
                },
            }
            jobs.append(
                (
                    state,
                    {
                        "kb_rule_" + key: choice(
                            f"Role of the named business rule {rule.name} in this request. Select explicitly requested rules; their dependencies will be resolved separately. A value illustration describes a stored field, not a new calculation; prefer the directly requested field.",
                            {
                                "metric": "Requested output, grouping or ordering concept",
                                "filter": "Required restriction or conditional statistic",
                                "unused": "Not directly requested",
                            },
                        )
                        for key, rule in page
                    },
                )
            )
        jobs.append(
            (
                {
                    "request": question,
                    "tables": {
                        d["name"]: d["description"]
                        + "; "
                        + ", ".join(c["name"] for c in d["columns"])
                        for d in datasets
                    },
                },
                {
                    **{
                        f"kb_number_{i}": choice(
                            f"What is the role of the literal {number} in the user's request? Do not reuse a limit or decimal precision as a filter threshold.",
                            {
                                "threshold": "Value in an explicit filtering condition",
                                "limit": "Number of results",
                                "precision": "Decimal places or display precision",
                                "constant": "Explicit fixed literal to return in the answer",
                                "other": "Date, identifier or another role",
                            },
                        )
                        for i, number in enumerate(numbers)
                    },
                    "kb_latest_table": choice(
                        "Does the objective require the latest observation for each entity before calculating the answer? Select the observation table, not its parent entity. None unless latest/most recent is requested.",
                        {
                            "none": "Use all observations",
                            **{d["name"]: d["name"] + "; " + d["description"] for d in datasets},
                        },
                    ),
                    "kb_count": choice(
                        "How many final output columns are requested? Count requested attributes and statistics, not internal formula operands.",
                        {str(i): str(i) for i in range(1, 11)},
                    ),
                    "kb_population": choice(
                        "Select the table defining the requested observation population, before joining formula operands.",
                        {d["name"]: d["name"] for d in datasets},
                    ),
                    "kb_join": choice(
                        "Should population records without matching related measurements be retained?",
                        {
                            "inner": "Only records with required measurements",
                            "left": "All population records including missing measurements",
                        },
                    ),
                },
            )
        )
        selected_roles = self.batch(tenant, jobs, "independent_grounding")
        roots = [key for key in rules if selected_roles["kb_rule_" + key] != "unused"]
        graph = RuleGraph(rules, candidates, columns, bindings=virtual)
        for identity in roots:
            try:
                graph.build(identity)
            except ValueError:
                pass
        resolve_relational_rules(self, tenant, graph, roots)
        selected_roles["kb_population"] = entity_contract(
            self, tenant, question, graph, datasets, selected_roles["kb_population"]
        )
        latest = latest_contract(
            self,
            tenant,
            question,
            selected_roles.get("kb_latest_table", "none"),
            datasets,
            fields,
            columns,
        )
        terms = {
            key: virtual.get(key, ref(key))
            for key in candidates
            if selected_roles["kb_field_" + key] in {"output", "filter", "output_filter"}
        }
        root_name = selected_roles["kb_population"]
        names_by_id = {d["id"]: d["name"] for d in datasets}
        reachable = {root_name}
        while True:
            before = set(reachable)
            for dataset in datasets:
                for link in dataset["links"]:
                    target = names_by_id.get(link["target_id"])
                    if target and (dataset["name"] in reachable or target in reachable):
                        reachable.update((dataset["name"], target))
            if before == reachable:
                break
        terms = {key: term for key, term in terms.items() if candidates[key].table in reachable}
        scores = {}
        for key, field in candidates.items():
            probabilities = self.answers.get("kb_field_" + key, {}).get("probabilities", {})
            scores[key] = probabilities.get("output", 0) + probabilities.get("output_filter", 0)
        alternatives = sorted(
            (key for key in candidates if candidates[key].table in reachable and scores[key] > 0),
            key=scores.get,
            reverse=True,
        )
        for key in alternatives[: min(32, max(8, int(selected_roles["kb_count"]) * 3))]:
            terms.setdefault(key, virtual.get(key, ref(key)))
        for key, field in fields.items():
            if field.table == root_name and columns[key].unit == "identifier":
                terms.setdefault(key, ref(key))
        labels = {
            key: candidates[key].label
            + ("; declared primary key" if columns[key].unit == "identifier" else "")
            for key in terms
        }
        for identity in roots:
            try:
                graph.build(identity)
                terms["k" + identity] = ref("k" + identity)
                labels["k" + identity] = rules[identity].name
            except ValueError:
                terms["k" + identity] = value(None)
                labels["k" + identity] = (
                    rules[identity].name
                    + " (unresolved definition; returns NULL pending correction)"
                )
        if not terms:
            root = next(d for d in datasets if d["name"] == selected_roles["kb_population"])
            for key, field in fields.items():
                if field.table == root["name"] and field.name in root["primary_key"]:
                    terms[key], labels[key] = ref(key), field.label
        for i, number in enumerate(numbers):
            if selected_roles.get(f"kb_number_{i}") == "constant":
                terms[f"c{i}"] = value(number)
                labels[f"c{i}"] = f"Explicit fixed answer constant {number}"
        terms["rows"] = value(1)
        labels["rows"] = "One source record (for count or percentage of population)"
        state = {
            "request": question,
            "available_operands": labels,
            "business_rules": [
                {"name": rules[key].name, "definition": rules[key].definition}
                for key in graph.terms
            ],
            "metric_grain": "Rules containing scoped sums/counts already return a statistic per entity; return that value unchanged unless the question asks for another aggregation across entities",
        }
        count = int(selected_roles["kb_count"])
        questions = {}
        for index in range(count):
            questions[f"kb_output_{index}"] = choice(
                f"Operand for final output column {index + 1} of {count}, in the user's requested order. Return the explicitly requested attribute or named concept, even when also used in a formula. An unspecified unique ID prefers the population primary key. Do not substitute a constant, metric or foreign key for an ID.",
                labels,
            )
        for key, term in terms.items():
            if term.type(columns).kind == "boolean":
                questions["kb_filter_" + key] = choice(
                    f"How should rule {labels[key]} affect the population? Conditional counts/proportions must retain nonmatching rows in their denominator.",
                    {
                        "restrict": "Retain only rows satisfying this rule",
                        "conditional": "Use only in conditional statistics or category; retain all source rows",
                        "unused": "Not required",
                    },
                )
        for index in range(2):
            questions[f"kb_sort_{index}"] = choice(
                f"Final ordering key {index + 1}; none if absent. Keys refer to final output positions.",
                {
                    "none": "No additional ordering",
                    **{str(i): f"Output column {i + 1}" for i in range(count)},
                },
            )
            questions[f"kb_direction_{index}"] = choice(
                f"Direction for ordering key {index + 1}",
                {"asc": "Ascending", "desc": "Descending"},
            )
        predicate_candidates = {}
        for key, term in terms.items():
            kind = term.type(columns).kind
            if key == "rows" or kind not in {"number", "integer"}:
                continue
            thresholds = [
                n
                for i, n in enumerate(numbers)
                if selected_roles.get(f"kb_number_{i}") == "threshold"
            ]
            options = predicate_options(key, kind, thresholds[:8])
            predicate_candidates[key] = options
            questions["kb_predicate_" + key] = choice(
                f"Required literal restriction on {labels[key]} after this value is computed, before the final report aggregation. Select only an explicitly requested condition on this exact value. A top-N limit or rounding precision is not a threshold. None if absent.",
                {candidate: label for candidate, (label, _) in options.items()},
            )
        questions["kb_limit"] = choice(
            "Final row limit explicitly requested; exclude decimal precision, dates and formula thresholds.",
            {
                "none": "No limit",
                **{str(int(n)): str(int(n)) for n in numbers if n == int(n) and 1 <= n <= 1000},
            },
        )
        answers = self.ask(tenant, state, questions, "parallel_output_contracts")
        window_kinds = {
            "percent_rank": "percentile rank",
            "quartile": "quartile",
            "avg": "population mean alongside individual rows",
            "sum": "population total alongside individual rows",
        }
        ownership = self.ask(
            tenant,
            {
                "request": question,
                "output_columns": count,
                "already_compiled_window_rules": [
                    rules[k].name
                    for k in roots
                    if k in graph.terms and graph.terms[k].columns() & graph.windows.keys()
                ],
            },
            {
                "kb_window_owner_" + kind: choice(
                    f"Which ONE requested output position explicitly asks for a NEW {label}? Count the requested columns in order. Select none if this operation is not requested, or if an already compiled named rule provides it. Ordinary source values are not implicitly transformed because another column asks for a rank or average.",
                    {
                        "none": "No new window of this kind",
                        **{str(i): f"Requested output column {i + 1}" for i in range(count)},
                    },
                )
                for kind, label in window_kinds.items()
            },
            "independent_window_ownership",
        )
        for i in range(count):
            answers[f"kb_window_kind_{i}"] = "none"
        for kind in window_kinds:
            slot = ownership["kb_window_owner_" + kind]
            if slot != "none":
                if answers[f"kb_window_kind_{slot}"] != "none":
                    raise ValueError("Competing window operations target the same answer slot")
                answers[f"kb_window_kind_{slot}"] = kind
        pending_windows = {
            i: answers[f"kb_window_kind_{i}"]
            for i in range(count)
            if answers[f"kb_window_kind_{i}"] != "none"
        }
        if pending_windows:
            numeric = {
                key: label
                for key, label in labels.items()
                if terms[key].type(columns).kind in {"number", "integer"}
                and terms[key].type(columns).unit != "identifier"
                and key != "rows"
            }
            bound = self.ask(
                tenant,
                state,
                {
                    f"kb_window_measure_{i}": choice(
                        f"Underlying measured value for requested output {i + 1}, whose operation is {kind}. Prefer the explicitly defined business formula when it is named; do not substitute a similarly named stored approximation.",
                        numeric,
                    )
                    for i, kind in pending_windows.items()
                },
                "window_operand_binding",
            )
            for i in pending_windows:
                original = self.answers.get(f"kb_output_{i}", {})
                key = original.get("_selection", bound[f"kb_window_measure_{i}"])
                answers[f"kb_output_{i}"] = key
                self.answers[f"kb_output_{i}"] = {**original, "probabilities": {key: 1.0}}

        questions = {}
        for index in range(count):
            key = answers[f"kb_output_{index}"]
            column = terms[key].type(columns)
            allowed = {"value", "count", "count_distinct", "share"}
            if column.kind in {"integer", "number"} and column.unit != "identifier":
                allowed.update({"avg", "sum", "min", "max"})
            if column.kind == "boolean":
                allowed.update({"sum", "conditional_share"})
            if key.startswith("c") or answers.get(f"kb_window_kind_{index}", "none") != "none":
                allowed = {"value"}
            questions[f"kb_agg_{index}"] = choice(
                f"Required aggregation of output {index + 1}: {labels[key]}. Scope is this operand only. Return a stored count/quantity as value unless the request explicitly aggregates across observations; counting source rows is a different quantity.",
                {k: v for k, v in AGGREGATES.items() if k in allowed},
            )
            questions[f"kb_round_{index}"] = choice(
                f"Explicit rounding of output {index + 1}: {labels[key]}. Only requested numeric statistics need rounding, never identifiers or labels.",
                {"none": "No rounding", **{str(i): str(i) + " decimal places" for i in range(7)}}
                if column.kind in {"integer", "number", "boolean"}
                and column.unit != "identifier"
                and re.search(r"\bround(?:ed|ing)?\b|decimal|小数|四舍五入", question, re.I)
                else {"none": "Not a measured numeric output"},
            )
        answers.update(
            self.ask(
                tenant,
                {
                    **state,
                    "output_bindings": {
                        str(i + 1): labels[answers[f"kb_output_{i}"]] for i in range(count)
                    },
                },
                questions,
                "typed_aggregation_contracts",
            )
        )
        bindings = self.assign_outputs(answers, count, terms, columns)
        answers.update(bindings)
        output_keys = {i: answers[f"kb_output_{i}"] for i in range(count)}
        window_jobs = [
            (
                {
                    "request": question,
                    "output_position": i + 1,
                    "operation": answers[f"kb_window_kind_{i}"],
                    "measure": labels[output_keys[i]],
                },
                {
                    f"kb_partition_{i}": choice(
                        "Comparison groups for THIS statistic. If the user asks for a mean for each category/sentiment/type while retaining individual rows, partition by that category. A global percentile has no partition. Bind this operation independently of other output windows.",
                        {
                            "none": "Whole population",
                            **{k: f.label for k, f in candidates.items() if f.table in reachable},
                        },
                    )
                },
            )
            for i in range(count)
            if answers.get(f"kb_window_kind_{i}", "none") != "none"
        ]
        window_answers = (
            self.batch(tenant, window_jobs, "independent_window_partitions") if window_jobs else {}
        )
        for i in range(count):
            kind = answers.get(f"kb_window_kind_{i}", "none")
            if kind == "none":
                continue
            key = output_keys[i]
            if (
                key.startswith("k")
                and graph.terms.get(key[1:], value(None)).columns() & graph.windows.keys()
            ):
                continue
            partition = window_answers[f"kb_partition_{i}"]
            term = graph.register_window(
                kind,
                terms[key],
                [] if partition == "none" else [virtual.get(partition, ref(partition))],
            )
            output_keys[i] = term.args[0]
            terms[term.args[0]] = term
        used = set(output_keys.values())
        filters = [key for key in terms if answers.get("kb_filter_" + key) == "restrict"]
        used.update(filters)
        predicates = [
            options[answers["kb_predicate_" + key]][1]
            for key, options in predicate_candidates.items()
            if answers["kb_predicate_" + key] != "none"
        ]
        used.update(set().union(*(term.columns() for term in predicates)))
        needed = set().union(*(terms[key].columns() for key in used))
        formula_keys = set()
        pending = set(needed)
        while pending:
            key = pending.pop()
            if key.startswith("k") and key[1:] in graph.terms:
                children = graph.terms[key[1:]].columns()
            elif key in graph.windows:
                _, operand, partition, _ = graph.windows[key]
                children = operand.columns() | set().union(*(item.columns() for item in partition))
            else:
                continue
            formula_keys.add(key)
            needed.update(children)
            pending.update(children - formula_keys)
        needed.difference_update(formula_keys)
        reduction_keys = needed & graph.reductions.keys()
        needed.difference_update(reduction_keys)
        root = selected_roles["kb_population"]
        root_keys = next(d["primary_key"] for d in datasets if d["name"] == root)
        entity = [
            key for key, field in fields.items() if field.table == root and field.name in root_keys
        ]
        if reduction_keys:
            if len(entity) != 1:
                raise ValueError(
                    "Scoped aggregate requires an unambiguous single-column entity key"
                )
            needed.update(entity)
        if latest:
            needed.add(next(key for key, field in fields.items() if field.table == latest["table"]))
        if not needed:
            needed = {next(key for key, field in fields.items() if field.table == root)}
        by_id = {d["id"]: d for d in datasets}
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
        all_links = links
        links = self.resolve_paths(
            tenant, question, root, {fields[key].table for key in needed}, links
        )
        source, aliases, edges = Program(
            root,
            fields,
            links,
            outputs=sorted(needed),
            family="absence" if selected_roles["kb_join"] == "left" else "rows",
        ).base()
        source = source.select(
            *[
                exp.alias_(fields[key].expression(aliases), key, quoted=True)
                for key in sorted(needed)
            ]
        )
        dag = StageDAG()
        source_bindings = {}
        if latest:
            source_bindings[latest["table"]] = latest_source(dag, **latest)
        node = dag.source(
            source, {key: columns[key] for key in sorted(needed)}, bindings=source_bindings
        )
        for key in sorted(reduction_keys):
            aggregate_table, reducer, operand, domain_bindings = graph.reductions[key]
            branch_fields = set(entity) | (
                operand.columns()
                if operand
                else {next(k for k, f in fields.items() if f.table == aggregate_table)}
            )
            branch_links = self.resolve_paths(
                tenant,
                question,
                root,
                {aggregate_table},
                all_links,
                domain_bindings=domain_bindings,
                fields=fields,
                entity=entity,
                primary_keys={d["name"]: d["primary_key"] for d in datasets},
            )
            query, branch_aliases, _ = Program(
                root, fields, branch_links, outputs=sorted(branch_fields)
            ).base()
            query = query.select(
                *[
                    exp.alias_(fields[k].expression(branch_aliases), k, quoted=True)
                    for k in sorted(branch_fields)
                ]
            )
            branch = dag.source(
                query,
                {k: columns[k] for k in sorted(branch_fields)},
                description="Independent source for " + reducer + " over " + aggregate_table,
                bindings={
                    name: stage for name, stage in source_bindings.items() if name in branch_aliases
                },
            )
            branch = dag.aggregate(
                branch, entity, {key: op(reducer, *([operand] if operand else []))}
            )
            node = dag.join(
                node,
                branch,
                [(entity[0], entity[0])],
                outputs={
                    **{k: ref("l." + k) for k in dag.nodes[node].columns},
                    key: op("case", op("is_null", ref("r." + key)), value(0), ref("r." + key))
                    if reducer == "count"
                    else ref("r." + key),
                },
                how="left",
            )
        restrictions = [*(terms[key] for key in filters), *predicates]
        scopes = window_scopes(self, tenant, question, graph, restrictions)
        node = compile_values(
            dag, node, graph, formula_keys, restrictions=restrictions, scopes=scopes
        )
        node = dag.project(
            node,
            {key: terms[key] for key in sorted(used)},
            description="Resolve shared business formulas before filtering or aggregation",
        )
        for key in filters:
            node = dag.filter(node, ref(key))
        for predicate in predicates:
            node = dag.filter(node, predicate, "Apply a required restriction on resolved values")
        groups, metrics, outputs, shares, rounding = [], {}, {}, {}, {}
        for index in range(count):
            key, kind = output_keys[index], answers[f"kb_agg_{index}"]
            target = "result_" + str(index + 1)
            operand = ref(key)
            is_boolean = dag.nodes[node].columns[key].kind == "boolean"
            if is_boolean and kind in {"count", "sum", "conditional_share"}:
                operand = op(
                    "case",
                    operand,
                    value(1),
                    op("case", op("is_null", operand), value(None), value(0)),
                )
            if kind == "value":
                if key not in groups:
                    groups.append(key)
                outputs[target] = ref(key)
            else:
                if kind == "conditional_share":
                    if not is_boolean:
                        raise ValueError("Conditional percentage requires a Boolean business rule")
                    metrics[target] = op("avg", operand)
                    outputs[target] = op("mul", ref(target), value(100))
                elif kind == "share":
                    metrics[target] = op("count")
                    shares[target] = "total_" + target
                    outputs[target] = op(
                        "mul", op("div", ref(target), ref(shares[target])), value(100)
                    )
                else:
                    metrics[target] = (
                        op("sum", operand)
                        if kind == "count" and is_boolean
                        else op(kind, *([] if kind == "count" else [operand]))
                    )
                    outputs[target] = ref(target)
            if answers[f"kb_round_{index}"] != "none":
                rounding[target] = int(answers[f"kb_round_{index}"])
        if metrics:
            node = dag.aggregate(node, groups, metrics)
        for target, total in shares.items():
            node = dag.window(node, total, op("sum", ref(target)))
        for key, digits in rounding.items():
            outputs[key] = op("round", outputs[key], value(digits))
        node = dag.project(
            node, outputs, description="Apply requested output calculations and precision"
        )
        order = []
        for index in range(2):
            key = answers[f"kb_sort_{index}"]
            if key != "none":
                item = ("result_" + str(int(key) + 1), answers[f"kb_direction_{index}"] == "desc")
                if item not in order:
                    order.append(item)
        node = dag.project(
            node,
            {key: ref(key) for key in outputs},
            order=order,
            limit=None if answers["kb_limit"] == "none" else int(answers["kb_limit"]),
        )
        sql = dag.compile(node).sql(dialect="postgres", pretty=True)
        issues = [
            {"code": "business_rule", "detail": rules[key].name + ": " + graph.errors[key]}
            for key in roots
            if key in graph.errors
        ]
        if getattr(self, "output_issue", None):
            issues.append({"code": "output_coverage", "detail": self.output_issue})
        audit = self.ask(
            tenant,
            {**state, "sql": sql, "stages": dag.describe(node), "unresolved_rules": issues},
            {
                "check_kb_complete": noul(
                    "Does this SQL satisfy every requested output, rule, filter, population, grouping, rounding and ordering requirement? Treat unresolved rules, missing calculations, and ambiguous relationship grain as incomplete."
                )
            },
            "audit",
        )
        if audit["check_kb_complete"] < 0.8:
            issues.append(
                {
                    "code": "business_rule_audit",
                    "detail": "The proposed rule graph may omit or misinterpret part of the request",
                }
            )
        return {
            "logical_sql": sql,
            "operation": "select",
            "knowledge_graph": {
                **graph.describe(roots),
                "rounds": self.rounds,
                "catalog_fields": len(fields),
                "output_candidates": len(terms),
                "relationships": [asdict(edge) for edge in edges],
            },
            "stage_dag": dag.describe(node),
            "_unresolved": issues,
            "_plan_bindings": bindings,
        }

    def assign_outputs(self, answers, count, terms, columns):
        distributions, fixed = [], {}
        for index in range(count):
            key = f"kb_output_{index}"
            answer = self.answers.get(key, {})
            kind = answers[f"kb_agg_{index}"]
            probabilities = answer.get("probabilities", {answers[key]: 1.0})
            candidates = {}
            for operand, probability in probabilities.items():
                column = terms[operand].type(columns)
                if kind == "value" and operand == "rows":
                    continue
                if kind in {"avg", "sum"} and (
                    column.kind not in {"number", "integer", "boolean"}
                    or column.unit == "identifier"
                ):
                    continue
                if kind == "conditional_share" and column.kind != "boolean":
                    continue
                if (
                    answers.get(f"kb_round_{index}", "none") != "none"
                    and kind == "value"
                    and (column.kind not in {"integer", "number"} or column.unit == "identifier")
                ):
                    continue
                candidates[
                    operand + "|" + kind + "|" + answers.get(f"kb_window_kind_{index}", "none")
                ] = probability
            distributions.append(candidates)
            if "_selection" in answer:
                fixed[index] = (
                    answer["_selection"]
                    + "|"
                    + kind
                    + "|"
                    + answers.get(f"kb_window_kind_{index}", "none")
                )
        try:
            selected = assign_unique(distributions, fixed=fixed)
        except ValueError as exc:
            if fixed:
                raise
            self.output_issue = str(exc)
            return {f"kb_output_{i}": answers[f"kb_output_{i}"] for i in range(count)}
        return {f"kb_output_{i}": key.rsplit("|", 2)[0] for i, key in enumerate(selected)}

    def resolve_paths(
        self,
        tenant,
        question,
        root,
        targets,
        links,
        *,
        domain_bindings=(),
        fields=None,
        entity=(),
        primary_keys=None,
    ):
        candidates = {}
        for target in sorted(targets - {root}):
            queue = deque([(root, [], {root})])
            paths = []
            visits = 0
            while queue and len(paths) < 16 and visits < 500:
                current, path, visited = queue.popleft()
                visits += 1
                if current == target:
                    paths.append(path)
                    continue
                if len(path) >= 5:
                    continue
                for edge in links:
                    if current not in {edge.source, edge.target}:
                        continue
                    other = edge.target if edge.source == current else edge.source
                    if other not in visited:
                        queue.append((other, [*path, edge], visited | {other}))
            if not paths:
                raise ValueError(f"No declared relationship path connects {root} to {target}")
            if domain_bindings:
                valid = []
                for path in paths:
                    try:
                        validate_bindings(domain_bindings, path, fields, root, entity, primary_keys)
                        valid.append(path)
                    except ValueError:
                        continue
                paths = valid
                if not paths:
                    raise ValueError(
                        "No declared relationship path satisfies the aggregate's entity bindings"
                    )
            minimum = min(len(path) for path in paths)
            candidates[target] = [path for path in paths if len(path) == minimum]
        questions = {
            "kb_path_" + target: choice(
                f"Which declared relationship path associates {root} with {target} at the requested observation grain? Choose the path whose intermediate entities match the objective. Equal-length paths are not semantically interchangeable.",
                {
                    str(i): "; ".join(
                        f"{e.source}.{e.source_column} = {e.target}.{e.target_column}" for e in path
                    )
                    for i, path in enumerate(paths)
                },
            )
            for target, paths in candidates.items()
        }
        answers = self.ask(
            tenant, {"request": question}, questions, "independent_relationship_paths"
        )
        selected_links = []
        for target, paths in candidates.items():
            for edge in paths[int(answers["kb_path_" + target])]:
                siblings = [
                    e
                    for e in links
                    if edge.constraint_id
                    and e.constraint_id == edge.constraint_id
                    and e.source == edge.source
                    and e.target == edge.target
                ] or [edge]
                for item in siblings:
                    if item not in selected_links:
                        selected_links.append(item)
        return selected_links
