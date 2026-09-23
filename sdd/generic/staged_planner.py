"""Factorized JEV contracts composed into a typed, shared relational stage graph."""

from dataclasses import asdict
import time

from sqlglot import exp
from sqlalchemy import func, select, text

from .assignment import assign_unique
from .output_contracts import output_tuple
from .jev import choice, noul, selected
from .parallel_planner import ParallelPlanner, chunks
from .plan_search import proposed_links
from .planning_review import PlanReviewRequired
from .relational import Field, Link, Program
from .sql import SQLService
from .stage_dag import Column, StageDAG, operation as op, ref, value


AGGREGATES = {
    "none": "Not required",
    "count": "Count all source records/events",
    "count_distinct": "Count unique values of the selected operand",
    "sum": "Total of the selected numeric operand",
    "avg": "Arithmetic mean of the selected numeric operand",
    "min": "Minimum operand value",
    "max": "Maximum operand value",
}
YES_NO = {"yes": "Required", "no": "Not required"}
TRANSFORMS = {
    "identity": "Use the stored value unchanged",
    "number": "Convert numeric text to a number",
    "clean_number": "Remove non-numeric characters (preserve decimal point and minus), then convert to a number",
    "year": "Extract calendar year from the date",
    "month": "Extract calendar month from the date",
    "age_years": "Current calendar year minus year of birth; year-based age, not exact birthday age",
    "suffix": "Final underscore-delimited identifier component",
}


class StagedPlanner(ParallelPlanner):
    def locked(self, key):
        return any(d["key"] == key and d.get("overridden") for d in self.decisions.decisions)

    def reconcile(self, contracts, key, selected):
        if contracts.get(key) != selected and self.locked(key):
            raise ValueError(f"User correction conflicts with dependent stage contract: {key}")
        contracts[key] = selected
        if not hasattr(self, "contract_bindings"):
            self.contract_bindings = {}
        self.contract_bindings[key] = selected

    def stage_catalog(self, tenant, question, datasets):
        jobs = []
        for index, page in enumerate(chunks(datasets, 32)):
            jobs.append(
                (
                    {"request": question},
                    {
                        f"dag_source_page_{index}": choice(
                            "Choose the primary observation population needed for the calculation, not a lookup table. None if this schema section has no relevant source.",
                            {
                                "none": "No source in this section",
                                **{
                                    d["name"]: d["name"]
                                    + " — "
                                    + d["description"]
                                    + "; fields: "
                                    + ", ".join(c["name"] for c in d["columns"])
                                    for d in page
                                },
                            },
                        )
                    },
                )
            )
        results = self.evaluate(tenant, jobs, "source_sections")
        candidates = list(
            dict.fromkeys(
                selected(a) for r in results for a in r["answers"].values() if selected(a) != "none"
            )
        )
        if not candidates:
            return self.focus(tenant, question, datasets)
        root = (
            candidates[0]
            if len(candidates) == 1
            else self.ask_factors(
                tenant,
                {"request": question},
                {
                    "dag_source": choice(
                        "Select the observation population across schema sections",
                        {k: k for k in candidates},
                    )
                },
                "source_reconciliation",
            )["dag_source"]
        )
        local = []
        for dataset in datasets:
            copied = {**dataset, "links": list(dataset["links"])}
            if not copied["primary_key"]:
                identifiers = [
                    c for c in dataset["columns"] if c["name"].casefold() in {"id", "code", "key"}
                ]
                for column in identifiers[:1]:
                    with self.catalog.db.transaction(tenant) as connection:
                        if self.catalog.db.engine.dialect.name == "postgresql":
                            connection.execute(text("SET LOCAL statement_timeout = '1000ms'"))
                        source = self.catalog.table(dataset, connection)
                        count, present, distinct = connection.execute(
                            select(
                                func.count(),
                                func.count(source.c[column["name"]]),
                                func.count(func.distinct(source.c[column["name"]])),
                            ).select_from(source)
                        ).one()
                    if count and count == present == distinct:
                        copied["primary_key"] = [column["name"]]
                        copied["_observed_key"] = True
            local.append(copied)
        by_id = {d["id"]: d for d in local}
        by_name = {d["name"]: d for d in local}
        approved = [
            Link(
                d["name"],
                by_id[link["target_id"]]["name"],
                link["source_column"],
                link["target_column"],
                constraint_id=link.get("constraint_id"),
            )
            for d in local
            for link in d["links"]
            if link["target_id"] in by_id
        ]
        connected, inferred = {root}, []
        for _ in range(4):
            ordered = sorted(local, key=lambda d: (d["name"] not in connected, d["name"]))
            hypotheses = proposed_links(self.catalog, tenant, ordered, [*approved, *inferred])
            inferred.extend(
                link for link in hypotheses if link.source in connected or link.target in connected
            )
            expanded = set(connected)
            for edge in [*approved, *inferred]:
                if edge.source in connected or edge.target in connected:
                    expanded.update((edge.source, edge.target))
            if expanded == connected:
                break
            connected = expanded
        for link in inferred:
            by_name[link.source]["links"].append(
                {
                    "target_id": by_name[link.target]["id"],
                    "source_column": link.source_column,
                    "target_column": link.target_column,
                    "inferred": True,
                    "cast_source": link.cast_source,
                    "cast_target": link.cast_target,
                }
            )
        focused = [d for d in local if d["name"] in connected]
        self.graph["schema"]["source_component"] = {
            "root": root,
            "tables": sorted(connected),
            "inferred_relationships": len(inferred),
        }
        return self.focus(tenant, question, focused)

    def ask_factors(self, tenant, state, questions, phase):
        fixed = {
            k: next(iter(q["criteria"]))
            for k, q in questions.items()
            if q["type"] == "choice" and len(q["criteria"]) == 1
        }
        questions = {k: q for k, q in questions.items() if k not in fixed}
        if fixed:
            self.graph.setdefault("deduced_choices", {}).update(fixed)
        jobs = [(state, dict(page)) for page in chunks(questions.items(), 12)]
        results = self.evaluate(tenant, jobs, phase)
        self.trace.extend(results)
        return {
            **fixed,
            **{
                key: selected(answer) if answer["type"] == "choice" else answer["noul"]
                for result in results
                for key, answer in result["answers"].items()
            },
        }

    def plan(self, tenant, question, datasets, legacy):
        self.request = question
        started = time.perf_counter()
        from .cohort_planner import try_cohort

        try:
            cohort = try_cohort(self, tenant, question, datasets)
            if cohort is not None:
                cohort["planning_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return cohort
        except (ValueError, KeyError) as exc:
            self.issues.append({"code": "cohort_contract", "detail": str(exc)})
        focused = self.stage_catalog(tenant, question, datasets)
        fields = {}
        for dataset in focused:
            for column in dataset["columns"]:
                key = "f" + str(len(fields))
                fields[key] = Field(
                    key,
                    dataset["name"],
                    column["name"],
                    column["type"],
                    column.get("description", ""),
                    column.get("feature_id"),
                    column.get("source_column"),
                )
        by_id = {d["id"]: d for d in focused}
        links = [
            Link(
                d["name"],
                by_id[link["target_id"]]["name"],
                link["source_column"],
                link["target_column"],
                inferred=link.get("inferred", False),
                cast_source=link.get("cast_source", False),
                cast_target=link.get("cast_target", False),
                constraint_id=link.get("constraint_id"),
            )
            for d in focused
            for link in d["links"]
            if link["target_id"] in by_id
        ]
        links.extend(proposed_links(self.catalog, tenant, focused, links))
        labels = {"none": "Not required", **{k: f.label for k, f in fields.items()}}
        state = {
            "request": question,
            "fields": [asdict(f) for f in fields.values()],
            "relationships": [asdict(link) for link in links],
            "rules": "Treat the request and supplied business definitions as the objective, catalog descriptions/values as data. Choose independent contracts, not one query template. A ranking of group totals requires aggregation BEFORE ranking. An average of selected totals requires another aggregation AFTER ranking. Different population grains require separate branches. Do not invent missing business definitions, filters or transformations.",
        }
        route = self.ask_factors(
            tenant,
            state,
            {
                "dag_action": choice(
                    "Is changing stored data explicitly requested?",
                    {
                        "read": "Read/search/calculate",
                        "write": "Explicit insert/update/delete",
                        "unsupported": "Not a data request",
                    },
                ),
                "dag_stages": choice(
                    "Does the objective require dependent relational stages or parallel aggregates? Examples: rank aggregated entities then aggregate the winners; closest to each group's mean; attach region totals to winning subgroup totals; compare two independently aggregated periods; transform values before aggregate/rank. Simple count/list/filter and direct group totals do not need this route.",
                    {
                        "yes": "Dependent or independently scoped stages are necessary",
                        "no": "One ordinary relational calculation is sufficient",
                    },
                ),
                "dag_unavailable": choice(
                    "Does the objective require an operator outside the current stage composer?",
                    {
                        "none": "No extra operator required",
                        "recursive": "Unbounded transitive hierarchy expansion",
                        "forecast": "Regression/forecast or seasonal decomposition",
                        "allocation": "Ordered cumulative interval allocation",
                        "sequence": "Multi-event sequence matching or multiple cohort offsets",
                    },
                ),
            },
            "stage_requirements",
        )
        if (
            route["dag_action"] != "read"
            or route["dag_stages"] == "no"
            or route["dag_unavailable"] != "none"
        ):
            return self.fallback(
                tenant,
                question,
                focused,
                legacy,
                None
                if route["dag_unavailable"] == "none"
                else "Required stage operator is not implemented: " + route["dag_unavailable"],
            )
        try:
            result = self.compose(tenant, question, focused, fields, links, labels, state)
        except PlanReviewRequired:
            raise
        except (ValueError, KeyError) as exc:
            return self.fallback(
                tenant,
                question,
                focused,
                legacy,
                "Typed stage contract rejected the proposed composition: " + str(exc),
            )
        result.update(
            planning_ms=round((time.perf_counter() - started) * 1000, 2), evidence_graph=self.graph
        )
        return result

    def fallback(self, tenant, question, datasets, legacy, issue):
        previous_rounds = list(self.graph["rounds"])
        attempt = {"rounds": previous_rounds, "fallback_reason": issue}
        dag = getattr(self, "stage_graph", None)
        if dag and dag.nodes:
            target = next(reversed(dag.nodes))
            attempt["rejected_dag"] = dag.describe(target)
            attempt["partial_sql"] = dag.compile(target).sql(dialect="postgres", pretty=True)
        planner = ParallelPlanner(self.catalog, self.decisions)
        try:
            plan = planner.plan(tenant, question, datasets, legacy)
        except PlanReviewRequired as exc:
            plan = exc.plan
            if issue:
                plan.setdefault("_unresolved", []).append(
                    {"code": "stage_contract", "detail": issue}
                )
            plan["stage_attempt"] = attempt
            raise PlanReviewRequired(plan, str(exc)) from exc
        if issue:
            plan.setdefault("_unresolved", []).append({"code": "stage_contract", "detail": issue})
        plan["stage_attempt"] = attempt
        return plan

    def compose(self, tenant, question, datasets, fields, links, labels, state):
        from .planner import literals

        numbers = list(
            dict.fromkeys(
                int(n) if n == int(n) else float(n) for n in literals(question)["numbers"]
            )
        )
        q = {
            "dag_root": choice(
                "Which fact/source table supplies the rows measured before any grouping?",
                {
                    "none": "Unresolved",
                    **{d["name"]: d["description"] or d["name"] for d in datasets},
                },
            ),
            "dag_group": choice(
                "Does the PRIMARY branch rank/return a statistic of MULTIPLE observations per entity, such as total sales per product? Choose no for individual people/records compared to a mean; that mean belongs in a separate reference branch and must not collapse those people/records.",
                YES_NO,
            ),
            "dag_rank": choice(
                "Must rows/groups be selected by rank, closest/farthest, top/bottom or an extremum?",
                YES_NO,
            ),
            "dag_comparison": choice(
                "Must each primary observation or aggregate satisfy a comparison against an independently calculated reference value? 'Above average' is a strict greater-than filter. Merely computing a difference, ranking by distance, or displaying a reference does not restrict rows.",
                {
                    "none": "No reference-value restriction",
                    "gt": "Primary > reference",
                    "ge": "Primary >= reference",
                    "lt": "Primary < reference",
                    "le": "Primary <= reference",
                    "eq": "Primary = reference",
                    "ne": "Primary differs from reference",
                },
            ),
            "dag_rank_direction": choice(
                "Which ranking order is requested? Closest distance is smallest first.",
                {"asc": "Smallest/closest first", "desc": "Largest/highest/farthest first"},
            ),
            "dag_rank_ties": choice(
                "How should equal ranking values be handled?",
                {
                    "rank": "Include every row tied at the cutoff (competition rank)",
                    "dense_rank": "Top N distinct measure values, including ties",
                    "row_number": "Exactly N rows per partition, without expanding ties",
                },
            ),
            "dag_rank_count": choice(
                "Which RANK POSITIONS qualify? Highest, lowest or closest without a number means rank <= 1. This retains ALL ties when rank/dense_rank is used; it does not mean one physical row.",
                {
                    str(n): "Rank positions 1 through "
                    + str(n)
                    + "; include all ties allowed by the rank operator"
                    for n in dict.fromkeys(
                        [1, *[n for n in numbers if isinstance(n, int) and 1 <= n <= 1000]]
                    )
                }
                | {
                    "none": "Return EVERY ranked record, including records that are NOT closest/best. Only for a full ranked listing."
                },
            ),
            "dag_rank_basis": choice(
                "What value is ranked?",
                {
                    "primary": "Primary aggregate or raw measure",
                    "distance": "Absolute distance from a separately computed group/population mean",
                    "difference": "Signed primary minus reference metric",
                    "growth": "Percentage change: (primary-reference)/reference",
                },
            ),
            "dag_reference_scope": choice(
                "Is there an INDEPENDENT baseline or summary population in addition to the main ranking calculation? Examples: group mean used to judge individual observations; region totals alongside the winning representatives; nationwide mean compared to local means. An average of the top-N totals is a FINAL outer aggregate and needs NO reference branch.",
                {
                    "none": "No reference/summary branch",
                    "global": "A scalar over the whole source population",
                    "groups": "Separate reference for each explicitly requested subgroup; individual expenses/readings alone are not subgroups",
                    "other_period": "Same grouping, independently filtered to the comparison period",
                },
            ),
            "dag_outer": choice(
                "Does the final answer REDUCE the selected winners/intermediate rows to a summary number? If the answer is a list of people/entities and their individual attributes, choose none even when a mean was used to select them. Only choose an aggregate when the requested answer itself summarizes selected values.",
                {
                    "none": "Return individual winning/intermediate rows",
                    "avg": "Average selected intermediate values",
                    "sum": "Sum selected intermediate values",
                    "count": "Count selected intermediate rows",
                    "min": "Minimum intermediate value",
                    "max": "Maximum intermediate value",
                },
            ),
        }
        for branch in ("primary", "reference"):
            for index in range(3):
                key = f"dag_{branch}_metric_{index}"
                q[key] = choice(
                    f"Aggregate measure {index + 1} for the {branch} branch. The primary first measure drives ranking; reference measures supply independently scoped comparisons or totals. Choose none for unused slots. Do not repeat a measure.",
                    AGGREGATES,
                )
                q[key + "_field"] = choice(
                    f"Operand for {branch} measure {index + 1}; when no aggregation, the primary first field is the raw ranking/calculation measure. COUNT(*) needs none. For COUNT DISTINCT use the entity key.",
                    labels,
                )
        intent = self.ask_factors(tenant, state, q, "independent_stage_contracts")
        if intent["dag_root"] == "none":
            raise ValueError("No source population")
        state = {**state, "stage_contracts": intent}
        usage = self.ask_factors(
            tenant,
            state,
            {
                "dag_table_" + str(i): choice(
                    "Does this table supply required measured values, outputs, restrictions or grouping attributes? Exclude extra fact tables that merely repeat an already available entity key. A bridge-only table supplies no additional query operands. Table: "
                    + d["name"]
                    + "; columns: "
                    + ", ".join(c["name"] for c in d["columns"]),
                    {
                        "needed": "Supplies required query operands",
                        "bridge": "Relationship bridge only",
                        "unused": "Unrelated to this objective",
                    },
                )
                for i, d in enumerate(datasets)
            },
            "table_operand_contracts",
        )
        active_tables = {
            d["name"] for i, d in enumerate(datasets) if usage["dag_table_" + str(i)] == "needed"
        } | {intent["dag_root"]}
        active_fields = {k: f for k, f in fields.items() if f.table in active_tables}
        state = {**state, "operand_tables": sorted(active_tables)}
        roles = {}
        for key, field in active_fields.items():
            roles["dag_grain_" + key] = choice(
                f"Is {field.label} part of the PRIMARY intermediate grouping key before ranking? Include both entity identifiers/names and its parent ranking partition as needed. This is not the final output or time filter. If no primary aggregation, choose no.",
                YES_NO,
            )
            roles["dag_partition_" + key] = choice(
                f"Does {field.label} define a separate top-N/closest list? A ranking by this field does not make it a partition. Choose no if no separate lists are requested.",
                YES_NO,
            )
            roles["dag_reference_grain_" + key] = choice(
                f"Is {field.label} a grouping key of the independent reference/summary branch? For a whole-country/global average choose no for location and company. Regional totals group by region, not sales representative.",
                YES_NO,
            )
            roles["dag_filter_" + key] = choice(
                f"Does the request constrain the STORED values of {field.label} to explicit literals or a required non-null condition? Exclude ranking criteria and computed aggregate thresholds. Use only necessary row restrictions.",
                YES_NO,
            )
            roles["dag_transform_" + key] = choice(
                f"Is a deterministic representation change required on {field.label} before calculation/filter/output? Do not transform an identifier simply because it is numeric text. Date of birth becomes age only when age is requested.",
                TRANSFORMS
                if field.kind in {"text", "date", "datetime"}
                else {"identity": TRANSFORMS["identity"], "number": TRANSFORMS["number"]},
            )
            roles["dag_outer_grain_" + key] = choice(
                f"Does the FINAL summary require one result for each {field.label}? Keep explicitly requested each/per dimensions through the outer reduction; choose no only for one overall result across all values. This differs from the intermediate entity grain.",
                YES_NO,
            )
            roles["dag_return_" + key] = choice(
                f"Is the individual value of {field.label} explicitly returned in the final answer (not just counted/averaged/ranked)?",
                YES_NO,
            )
        selected_roles = self.ask_factors(tenant, state, roles, "field_contracts")
        for key in set(fields) - set(active_fields):
            for prefix in [
                "dag_grain_",
                "dag_partition_",
                "dag_reference_grain_",
                "dag_filter_",
                "dag_outer_grain_",
                "dag_return_",
            ]:
                selected_roles[prefix + key] = "no"
            selected_roles["dag_transform_" + key] = "identity"
        if intent["dag_reference_scope"] == "none" and not self.locked("dag_reference_scope"):
            scoped = self.ask_factors(
                tenant,
                {
                    "request": question,
                    "source_fields": {k: f.label for k, f in active_fields.items()},
                    "primary_group": [
                        fields[k].label for k in fields if selected_roles["dag_grain_" + k] == "yes"
                    ],
                    "candidate_summary_group": [
                        fields[k].label
                        for k in fields
                        if selected_roles["dag_reference_grain_" + k] == "yes"
                    ],
                },
                {
                    "dag_population_scope": choice(
                        "Do any requested quantities describe a DIFFERENT population from the ranked entities? Totals/counts for each parent group must cover all its source records, including losing entities. They require a separate group branch. A mean of selected winners alone is an outer reduction, not a separate population.",
                        {
                            "none": "Every quantity uses the same population, or only selected winners",
                            "groups": "Separate totals/counts/means per parent or reference group",
                            "global": "Separate whole-population scalar",
                            "other_period": "Separate comparison period at the same entity grain",
                        },
                    )
                },
                "population_scope_reconciliation",
            )
            self.reconcile(intent, "dag_reference_scope", scoped["dag_population_scope"])
        state = {**state, "stage_contracts": intent}
        if intent["dag_rank"] == "yes" and not self.locked("dag_group"):
            grain = self.ask_factors(
                tenant,
                {
                    "request": question,
                    "source": intent["dag_root"],
                    "fields": {k: f.label for k, f in active_fields.items()},
                },
                {
                    "dag_primary_scope": choice(
                        "What does ONE competitor in the ranking represent? Distinguish an individual stored event/record from an entity's combined observations. A representative's total sales requires combining all their orders before ranking; an individual person's salary or an individual reading stays at the original row grain. A separately calculated comparison mean does not change this choice.",
                        {
                            "rows": "One individual stored observation/person/event",
                            "groups": "One entity's aggregated observations, such as its total or count",
                        },
                    )
                },
                "primary_grain_reconciliation",
            )
            self.reconcile(
                intent, "dag_group", "yes" if grain["dag_primary_scope"] == "groups" else "no"
            )
        state = {**state, "stage_contracts": intent}
        identifiers = {(d["name"], name) for d in datasets for name in d["primary_key"]} | {
            (link.source, link.source_column) for link in links
        }
        operand_questions = {}
        for branch in ["primary", "reference"]:
            if branch == "reference" and intent["dag_reference_scope"] == "none":
                continue
            for i in range(3):
                key = f"dag_{branch}_metric_{i}"
                kind = intent[key]
                if branch == "primary" and intent["dag_group"] == "no":
                    if i != 0:
                        continue
                    kind = "none"
                if kind == "none" and not (branch == "primary" and i == 0):
                    continue
                if kind == "count":
                    self.reconcile(intent, key + "_field", "none")
                    continue
                choices = {"none": "No type-compatible operand in this catalog"}
                for field_key, field in active_fields.items():
                    transform = selected_roles["dag_transform_" + field_key]
                    numeric = field.kind in {"integer", "number"} or transform in {
                        "number",
                        "clean_number",
                        "year",
                        "month",
                        "age_years",
                    }
                    numeric_measure = kind in {"sum", "avg"} or (
                        branch == "primary" and i == 0 and intent["dag_rank_basis"] != "primary"
                    )
                    if numeric_measure and (
                        not numeric or (field.table, field.name) in identifiers
                    ):
                        continue
                    choices[field_key] = field.label + (
                        "; transformed with " + transform if transform != "identity" else ""
                    )
                original = intent[key + "_field"]
                if original != "none" and original in choices:
                    continue
                if self.locked(key + "_field"):
                    raise ValueError("User-corrected operand violates the stage type: " + key)
                operand_questions[key + "_operand"] = choice(
                    f"Resolve the typed operand of {branch} measure {i + 1}: {kind.upper()}. The first primary measure ranks the requested entities. Use the actual monetary/quantity measurement, never an identifier. Additional measures must be explicitly requested at this SAME grain, not just mentioned as a later outer mean.",
                    choices,
                )
        operands = self.ask_factors(
            tenant,
            {**state, "field_contracts": selected_roles},
            operand_questions,
            "typed_metric_operands",
        )
        for key, picked in operands.items():
            self.reconcile(intent, key.removesuffix("_operand") + "_field", picked)
        if intent["dag_reference_scope"] != "none":
            counts = self.ask_factors(
                tenant,
                state,
                {
                    "dag_reference_count": choice(
                        "Does the objective explicitly require the NUMBER OF SOURCE EVENTS/RECORDS for the independent reference/summary population, in addition to any amount total? For example a report may require both order count and amount. Do not confuse number of selected output rows with source event count.",
                        YES_NO,
                    )
                },
                "reference_measure_coverage",
            )
            intent["dag_reference_count"] = counts["dag_reference_count"]
        reconciled = {}
        if intent["dag_rank"] == "yes":
            reconciled = self.ask_factors(
                tenant,
                {"request": question, "fields": [asdict(f) for f in active_fields.values()]},
                {
                    "dag_rank_entity_field": choice(
                        "Which field identifies/describes the ENTITIES being ranked (the objects competing to be highest/lowest/closest)? This differs from the numeric ranking measure and the parent group. Prefer their requested display name when available.",
                        {
                            "none": "Raw observations without a named entity",
                            **{k: f.label for k, f in active_fields.items()},
                        },
                    ),
                    "dag_rank_partition_field": choice(
                        "Are winners determined separately FOR EACH parent category/group? Select that category. Include implicit 'highest ... in that region' and 'closest ... for their respective rank'. Choose none ONLY for one global competition.",
                        {
                            "none": "One global competition",
                            **{k: f.label for k, f in active_fields.items()},
                        },
                    ),
                },
                "ranking_grain_reconciliation",
            )
        time_fields = [
            k
            for k, f in active_fields.items()
            if selected_roles["dag_transform_" + k] == "year"
            or ("year" in f.name.casefold() and selected_roles["dag_filter_" + k] == "yes")
        ]
        if len(time_fields) > 1 and any(1000 <= n <= 3000 for n in numbers):
            clock = self.ask_factors(
                tenant,
                {"request": question},
                {
                    "dag_calendar_binding": choice(
                        "Which time representation supplies the requested YEAR restriction? Calendar year is extracted from the actual date; fiscal year is used only when the fiscal accounting year is requested. Birth year is not observation year. Choose none if no year filter is required.",
                        {
                            "none": "No observation-year restriction",
                            **{
                                k: active_fields[k].label
                                + "; transformation="
                                + selected_roles["dag_transform_" + k]
                                for k in time_fields
                            },
                        },
                    )
                },
                "time_representation",
            )
            chosen_clock = clock["dag_calendar_binding"]
            if chosen_clock != "none":
                for k in time_fields:
                    self.reconcile(
                        selected_roles, "dag_filter_" + k, "yes" if k == chosen_clock else "no"
                    )
                    if k != chosen_clock or intent["dag_reference_scope"] == "other_period":
                        self.reconcile(selected_roles, "dag_grain_" + k, "no")
                    if intent["dag_reference_scope"] == "other_period":
                        self.reconcile(selected_roles, "dag_reference_grain_" + k, "no")
        state = {**state, "field_contracts": selected_roles}
        groups = (
            [k for k in fields if selected_roles["dag_grain_" + k] == "yes"]
            if intent["dag_group"] == "yes"
            else []
        )
        partitions = (
            [k for k in fields if selected_roles["dag_partition_" + k] == "yes"]
            if intent["dag_rank"] == "yes"
            else []
        )
        reference_groups = (
            [k for k in fields if selected_roles["dag_reference_grain_" + k] == "yes"]
            if intent["dag_reference_scope"] in {"groups", "other_period"}
            else []
        )
        if (
            reference_groups
            and intent["dag_reference_scope"] == "groups"
            and not self.locked("dag_reference_scope")
        ):
            scope = self.ask_factors(
                tenant,
                {"request": question},
                {
                    "dag_reference_grouped": choice(
                        "Does the objective explicitly require a different reference average/total for each of these categories: "
                        + ", ".join(fields[k].label for k in reference_groups)
                        + "? Comparing each individual observation to an average does not itself request subgroup averages.",
                        {
                            "yes": "Separate reference within these categories",
                            "no": "One reference over the whole eligible population",
                        },
                    )
                },
                "reference_scope_reconciliation",
            )
            if scope["dag_reference_grouped"] == "no":
                reference_groups = []
                self.reconcile(intent, "dag_reference_scope", "global")
        returned = [k for k in fields if selected_roles["dag_return_" + k] == "yes"]
        transforms = {
            k: selected_roles["dag_transform_" + k]
            for k in fields
            if selected_roles["dag_transform_" + k] != "identity"
        }
        metric_fields = [
            v
            for k, v in intent.items()
            if k.endswith("_field")
            and v != "none"
            and ("primary_metric_0" in k or intent[k.removesuffix("_field")] != "none")
        ]
        if intent["dag_group"] == "yes":
            groups = list(
                dict.fromkeys(
                    [
                        *groups,
                        *[k for k in returned if k not in metric_fields],
                        *[
                            reconciled["dag_rank_entity_field"]
                            for _ in [0]
                            if reconciled.get("dag_rank_entity_field", "none") != "none"
                        ],
                    ]
                )
            )
        if reconciled.get("dag_rank_partition_field", "none") != "none":
            partitions = list(dict.fromkeys([*partitions, reconciled["dag_rank_partition_field"]]))
            if intent["dag_group"] == "yes":
                groups = list(dict.fromkeys([*groups, *partitions]))
        for key in groups:
            self.reconcile(selected_roles, "dag_grain_" + key, "yes")
        for key in partitions:
            self.reconcile(selected_roles, "dag_partition_" + key, "yes")
        state = {**state, "field_contracts": selected_roles}
        needed = set(
            groups + partitions + reference_groups + returned + metric_fields + list(transforms)
        )
        needed.update(k for k in fields if selected_roles["dag_filter_" + k] == "yes")
        if not needed:
            needed.add(next(k for k, f in fields.items() if f.table == intent["dag_root"]))
        query, aliases, edges = Program(
            intent["dag_root"], fields, links, outputs=sorted(needed)
        ).base()
        retained = {k: f for k, f in fields.items() if k in needed and f.table in aliases}
        query = query.select(
            *[exp.alias_(f.expression(aliases), k, quoted=True) for k, f in retained.items()]
        )
        primary_keys = {(d["name"], name) for d in datasets for name in d["primary_key"]}
        foreign_keys = {(edge.source, edge.source_column) for edge in links}
        columns = {
            k: Column(
                f.kind,
                f.label,
                True,
                "identifier" if (f.table, f.name) in primary_keys | foreign_keys else None,
                (f.table + "." + f.name,),
            )
            for k, f in retained.items()
        }
        dag = self.stage_graph = StageDAG()
        source = dag.source(query, columns)
        if transforms:
            source = dag.project(
                source,
                {k: op(kind, ref(k)) for k, kind in transforms.items()},
                keep=True,
                description="Normalize requested representations before arithmetic",
            )
        common, main_filters, reference_filters = self.filters(
            tenant,
            question,
            datasets,
            retained,
            selected_roles,
            dag.nodes[source].columns,
            state,
            intent,
            numbers,
        )
        if common:
            source = dag.filter(source, self.conjunction(common))
        primary_source = (
            dag.filter(source, self.conjunction(main_filters), "Primary branch population")
            if main_filters
            else source
        )
        reference_source = (
            dag.filter(
                source, self.conjunction(reference_filters), "Independent reference population"
            )
            if reference_filters
            else source
        )
        metric_labels = {}

        def metrics(branch):
            result = {}
            seen = set()
            for i in range(3):
                key = f"dag_{branch}_metric_{i}"
                kind, operand = intent[key], intent[key + "_field"]
                if kind == "none" or (kind, operand) in seen:
                    continue
                seen.add((kind, operand))
                if operand == "none" and kind != "count":
                    if i == 0:
                        raise ValueError("Aggregate has no typed operand")
                    self.issues.append(
                        {
                            "code": "stage_contract",
                            "detail": "Additional aggregate has no operand and was omitted",
                        }
                    )
                    continue
                term = op(kind) if kind == "count" else op(kind, ref(operand))
                name = ("m" if branch == "primary" else "b") + str(i)
                result[name] = term
                metric_labels[name] = f"{branch} {kind.upper()} of " + (
                    "source rows" if operand == "none" else dag.nodes[source].columns[operand].label
                )
            if (
                branch == "reference"
                and intent.get("dag_reference_count") == "yes"
                and not any(term.op == "count" for term in result.values())
            ):
                result["b_count"] = op("count")
                metric_labels["b_count"] = (
                    "COUNT(*) of ALL source events, including every losing/unselected entity"
                )
            branch_groups = groups if branch == "primary" else reference_groups
            grain_label = (
                ", ".join(fields[k].table + "." + fields[k].name for k in branch_groups)
                or "the entire source population"
            )
            for name in result:
                metric_labels[name] += "; one value per " + grain_label
                metric_labels[name] += (
                    "; covers ALL entities in this population BEFORE selecting winners"
                    if branch == "reference"
                    else "; covers only this entity/group's observations"
                )
            branch_filters = main_filters if branch == "primary" else reference_filters
            if branch_filters:
                for name in result:
                    metric_labels[name] += "; population: " + " AND ".join(
                        term.sql().sql() for term in branch_filters
                    )
            return result

        primary_metrics = metrics("primary") if intent["dag_group"] == "yes" else {}
        main = (
            dag.aggregate(primary_source, groups, primary_metrics)
            if intent["dag_group"] == "yes"
            else primary_source
        )
        primary_measure = (
            "m0" if intent["dag_group"] == "yes" else intent["dag_primary_metric_0_field"]
        )
        if primary_measure == "none" or primary_measure not in dag.nodes[main].columns:
            raise ValueError("Primary calculation lacks a value at its declared grain")
        if intent["dag_reference_scope"] != "none":
            other = dag.aggregate(
                reference_source,
                reference_groups,
                metrics("reference"),
                "Independent reference aggregates before selecting winners",
            )
            missing = set(reference_groups) - set(dag.nodes[main].columns)
            if missing:
                raise ValueError("Reference and primary branches have incompatible grouping keys")
            outputs = {k: ref("l." + k) for k in dag.nodes[main].columns}
            outputs.update(
                {k: ref("r." + k) for k in dag.nodes[other].columns if k not in reference_groups}
            )
            main = dag.join(
                main,
                other,
                [(k, k) for k in reference_groups],
                outputs=outputs,
                how="inner" if reference_groups else "cross",
            )
        if intent["dag_comparison"] != "none":
            if "b0" not in dag.nodes[main].columns:
                raise ValueError("A required comparison has no resolved reference population")
            main = dag.filter(
                main,
                op(intent["dag_comparison"], ref(primary_measure), ref("b0")),
                "Apply the required comparison after both populations are available",
            )
        rank_measure = primary_measure
        basis = intent["dag_rank_basis"]
        if basis != "primary":
            delta = op("sub", ref(primary_measure), ref("b0"))
            expression = (
                op("abs", delta)
                if basis == "distance"
                else op("mul", value(100), op("div", delta, ref("b0")))
                if basis == "growth"
                else delta
            )
            main = dag.project(
                main,
                {"comparison": expression},
                keep=True,
                description="Derive comparison only after independent aggregates meet",
            )
            metric_labels["comparison"] = {
                "distance": "Absolute distance from reference mean",
                "growth": "Percentage increase over reference",
                "difference": "Primary minus reference",
            }[basis]
            rank_measure = "comparison"
        if intent["dag_rank"] == "yes" and basis == "primary":
            ranking_options = {
                k: metric_labels.get(k, column.label)
                for k, column in dag.nodes[main].columns.items()
                if column.kind in {"integer", "number"}
                and column.unit != "identifier"
                and not k.startswith("b")
                and k not in groups + partitions
            }
            if ranking_options:
                chosen_rank = self.ask_factors(
                    tenant,
                    {"request": question, "available_measures": ranking_options},
                    {
                        "dag_rank_operand": choice(
                            "Which VALUE determines who wins? This is not necessarily the first returned statistic. A count of deliveries and total delivered quantity are different. Select the actual requested highest/lowest measure at the competitors' grain.",
                            ranking_options,
                        )
                    },
                    "ranking_operand",
                )
                rank_measure = chosen_rank["dag_rank_operand"]
                primary_measure = rank_measure
        if intent["dag_rank"] == "yes":
            main = dag.window(
                main,
                "position",
                op(intent["dag_rank_ties"]),
                partition=partitions,
                order=[(rank_measure, intent["dag_rank_direction"] == "desc")],
                description="Rank at the intermediate grain, separately inside each partition",
            )
            if intent["dag_rank_count"] != "none":
                main = dag.filter(
                    main,
                    op("le", ref("position"), value(int(intent["dag_rank_count"]))),
                    "Keep all required winning positions",
                )
        if intent["dag_outer"] != "none":
            outer = intent["dag_outer"]
            main = dag.aggregate(
                main,
                [
                    k
                    for k in fields
                    if selected_roles["dag_outer_grain_" + k] == "yes"
                    and k in dag.nodes[main].columns
                ],
                {
                    "final_metric": op(outer)
                    if outer == "count"
                    else op(outer, ref(primary_measure))
                },
                "Aggregate selected intermediate rows at final answer grain",
            )
            metric_labels["final_metric"] = (
                f"{outer.upper()} of selected {metric_labels.get(primary_measure, primary_measure)} values"
            )
        main = self.finish(tenant, state, dag, main, metric_labels, numbers)
        description = dag.describe(main)
        sql = dag.compile(main).sql(dialect="postgres", pretty=True)
        SQLService(self.catalog.db).prepare(tenant, sql)
        audits = []
        for level in description["layers"]:
            jobs = []
            for identity in level:
                node = next(s for s in description["stages"] if s["id"] == identity)
                if node["operator"] in {"aggregate", "join", "window"}:
                    jobs.append(
                        (
                            {
                                "request": question,
                                "stage": node,
                                "parents": [
                                    next(s for s in description["stages"] if s["id"] == p)
                                    for p in node["inputs"]
                                ],
                            },
                            {
                                "check_dag_" + identity: noul(
                                    "Does this stage implement the required intermediate row meaning, population, units, grouping and tie policy without dropping a requirement or multiplying unrelated observations?"
                                )
                            },
                        )
                    )
            if jobs:
                results = self.evaluate(tenant, jobs, "stage_frontier_audit")
                self.trace.extend(results)
                audits.extend(
                    (k, a["noul"]) for result in results for k, a in result["answers"].items()
                )
        audit = self.ask_factors(
            tenant,
            {"request": question, "stage_contracts": description, "sql": sql},
            {
                "complete": noul(
                    "Does this complete SQL satisfy exactly the requested outputs, filters, intermediate and final grains, arithmetic, ordering and tie handling? An executable partial result is not complete."
                )
            },
            "stage_result_audit",
        )
        issues = [
            *self.issues,
            *[
                {"code": "stage_contract", "detail": key + " requires review"}
                for key, score in audits
                if score < 0.8
            ],
        ]
        for edge in edges:
            if edge.inferred:
                issues.append(
                    {
                        "code": "relationship_proposed",
                        "detail": f"Inferred read relationship: {edge.source}.{edge.source_column} = {edge.target}.{edge.target_column}",
                    }
                )
        plan = {
            "request": question,
            "operation": "select",
            "logical_sql": sql,
            "dataset_ids": [d["id"] for d in datasets],
            "model": self.decisions.model,
            "decision_trace": self.trace,
            "stage_dag": description,
            "_plan_bindings": {
                **getattr(self, "contract_bindings", {}),
                **getattr(self, "output_bindings", {}),
            },
            "evidence_graph": self.graph,
            "_unresolved": issues,
        }
        if audit["complete"] < 0.8:
            raise PlanReviewRequired(plan, "The composed stage graph needs review.")
        return plan

    @staticmethod
    def conjunction(terms):
        result = terms[0]
        for term in terms[1:]:
            result = op("and", result, term)
        return result

    def filters(self, tenant, question, datasets, fields, roles, columns, state, intent, numbers):
        active = [k for k in fields if roles["dag_filter_" + k] == "yes"]
        by_name = {d["name"]: d for d in datasets}
        choices, jobs = {}, {}
        for key in active:
            field = fields[key]
            options = {
                "none": ("No row filter", None),
                "not_null": ("Value must be available", op("not_null", ref(key))),
                "null": ("Value is missing", op("is_null", ref(key))),
            }
            if columns[key].kind in {"number", "integer"}:
                candidates = list(dict.fromkeys([*numbers, 0]))
                operations = {
                    "eq": "equals",
                    "ne": "does not equal",
                    "gt": "greater than",
                    "ge": "at least",
                    "lt": "less than",
                    "le": "at most",
                }
            else:
                candidates = self.domain(tenant, by_name[field.table], field.name)
                # Domain pages remain independent; no unverified predicate-shaped strings.
                exact = [v for v in candidates if str(v).casefold() in question.casefold()]
                candidates = list(dict.fromkeys([*exact, *candidates]))[:100]
                operations = {"eq": "equals", "ne": "does not equal"}
                if len(exact) > 1:
                    options["matched_set"] = (
                        "Any of these explicitly matched catalog values: "
                        + ", ".join(map(str, exact)),
                        op("in", ref(key), *[value(v) for v in exact]),
                    )

            for index, candidate in enumerate(candidates):
                for operator, label in operations.items():
                    options[f"v{index}_{operator}"] = (
                        label + " " + str(candidate),
                        op(operator, ref(key), value(candidate)),
                    )
            if columns[key].kind in {"number", "integer"}:
                from .cohort_planner import predicate_options

                options.update(predicate_options(key, columns[key].kind, candidates))
            choices[key] = options
            for page_index, page in enumerate(
                chunks([(k, v) for k, v in options.items() if k != "none"], 110)
            ):
                jobs[f"dag_predicate_{key}_{page_index}"] = choice(
                    f"Select the exact required restriction on {field.label}. When comparing periods, this PRIMARY branch uses the newer/current numerator period and the reference uses the earlier denominator. None if this page lacks it. Do not add restrictions merely because values appear in another field or a rank count.",
                    {"none": "No required predicate here", **{k: v[0] for k, v in page}},
                )
        answers = self.ask_factors(tenant, state, jobs, "grounded_predicate_pages") if jobs else {}
        selected_filters = {}
        for key in active:
            winners = [
                v
                for k, v in answers.items()
                if k.startswith("dag_predicate_" + key + "_") and v != "none"
            ]
            if len(winners) > 1:
                picked = self.ask_factors(
                    tenant,
                    state,
                    {
                        "dag_predicate_reconcile_" + key: choice(
                            "Select the single restriction intended for this field across dictionary pages.",
                            {k: choices[key][k][0] for k in winners},
                        )
                    },
                    "predicate_reconciliation",
                )
                winners = list(picked.values())
            if winners:
                selected_filters[key] = choices[key][winners[0]][1]
            else:
                self.issues.append(
                    {
                        "code": "stage_contract",
                        "detail": "Required literal restriction unresolved: " + fields[key].label,
                    }
                )
        if intent["dag_reference_scope"] == "other_period":
            reference_jobs = {}
            for key in active:
                for page_index, page in enumerate(chunks(list(choices[key].items()), 110)):
                    reference_jobs[f"dag_reference_predicate_{key}_{page_index}"] = choice(
                        f"Required restriction on {fields[key].label} for the REFERENCE/earlier denominator population. Preserve common restrictions, but choose its own comparison period. None if absent from this page.",
                        {
                            "none": "No reference restriction on this page",
                            **{k: v[0] for k, v in page if k != "none"},
                        },
                    )
            reference_answers = self.ask_factors(
                tenant, state, reference_jobs, "independent_reference_predicates"
            )
            reference_selected = {}
            for key in active:
                picks = [
                    v
                    for k, v in reference_answers.items()
                    if k.startswith("dag_reference_predicate_" + key + "_") and v != "none"
                ]
                if len(set(picks)) == 1:
                    reference_selected[key] = choices[key][picks[0]][1]
                elif len(set(picks)) > 1:
                    self.issues.append(
                        {
                            "code": "stage_contract",
                            "detail": "Reference predicate has conflicting candidates: "
                            + fields[key].label,
                        }
                    )
            common, primary, reference = [], [], []
            for key in sorted(set(selected_filters) | set(reference_selected)):
                left, right = selected_filters.get(key), reference_selected.get(key)
                if left == right:
                    common.append(left)
                else:
                    if left is not None:
                        primary.append(left)
                    if right is not None:
                        reference.append(right)
            return common, primary, reference
        scope_questions = (
            {
                "dag_predicate_scope_" + key: choice(
                    f"Which branch's population is restricted by {fields[key].label} {term.sql().sql()}? A location shortlist must not restrict an overall national average. Same-period restrictions apply to both branches unless a different comparison period is requested.",
                    {
                        "both": "Both primary and reference",
                        "primary": "Primary branch only",
                        "reference": "Reference branch only",
                    },
                )
                for key, term in selected_filters.items()
            }
            if intent["dag_reference_scope"] != "none"
            else {}
        )
        scopes = (
            self.ask_factors(tenant, state, scope_questions, "population_scope")
            if scope_questions
            else {}
        )
        groups = {"both": [], "primary": [], "reference": []}
        for key, term in selected_filters.items():
            groups[scopes.get("dag_predicate_scope_" + key, "both")].append(term)
        return groups["both"], groups["primary"], groups["reference"]

    def finish(self, tenant, state, dag, source, metric_labels, numbers):
        columns = dag.nodes[source].columns
        labels = {
            "none": "Not returned",
            **{k: metric_labels.get(k, v.label) for k, v in columns.items()},
        }
        required = [k for k in columns if state["field_contracts"].get("dag_return_" + k) == "yes"]
        counts = self.ask_factors(
            tenant,
            {
                "request": state["request"],
                "available_stage_columns": labels,
                "required_direct_attributes": required,
            },
            {
                "dag_output_count": choice(
                    "How many columns does the final answer explicitly request? Count requested attributes and returned statistics, excluding computation-only ranks, baselines and grouping keys. A summary per group needs that group's identifier plus the statistic.",
                    {
                        str(n): str(n)
                        for n in range(max(1, len(required)), min(10, len(columns)) + 1)
                    },
                )
            },
            "output_cardinality",
        )
        count = int(counts["dag_output_count"])
        q = {
            f"dag_output_{i}": choice(
                f"Final output column {i + 1} of exactly {count}, in the requested order. Include only requested answer values, not intermediate filters/ranks. Every position must be a distinct requested value.",
                {k: v for k, v in labels.items() if k != "none"},
            )
            for i in range(count)
        }
        q.update(
            {
                "dag_distinct": choice(
                    "Should duplicate final answer rows be removed?",
                    {
                        "yes": "Distinct requested entities/values",
                        "no": "Preserve the output stage's row multiplicity",
                    },
                ),
                "dag_sort": choice(
                    "Value by which the final result must be ordered, if requested. Ordering within rank windows is already applied.",
                    labels,
                ),
                "dag_sort_direction": choice(
                    "Final result order direction", {"asc": "Ascending", "desc": "Descending"}
                ),
                "dag_limit": choice(
                    "Overall final result limit, not per-partition ranking count or a date/threshold",
                    {
                        "none": "No overall limit",
                        **{
                            str(n): str(n) for n in numbers if isinstance(n, int) and 1 <= n <= 1000
                        },
                    },
                ),
            }
        )
        answer = self.ask_factors(
            tenant,
            {
                "request": state["request"],
                "available_stage_columns": labels,
                "output_grain": dag.nodes[source].grain,
                "required_direct_attributes": required,
            },
            q,
            "result_contract",
        )
        distributions, fixed = [], {}
        for i in range(count):
            key = f"dag_output_{i}"
            decision = next(
                (d for d in reversed(self.decisions.decisions) if d["key"] == key), None
            )
            distributions.append(
                {o["id"]: o["probability"] for o in decision["options"]}
                if decision
                else {answer[key]: 1.0}
            )
            if decision and decision.get("overridden"):
                fixed[i] = decision["selected"]
        outputs = assign_unique(distributions, required=required, fixed=fixed)
        outputs = output_tuple(
            self.ask_factors,
            tenant,
            state["request"],
            {k: v for k, v in labels.items() if k != "none"},
            outputs,
            prefix="dag_answer",
            locked=any(
                d.get("overridden") and d["key"].startswith(("dag_output_", "dag_return_"))
                for d in self.decisions.decisions
            ),
        )
        self.output_bindings = {f"dag_output_{i}": key for i, key in enumerate(outputs)}
        if not outputs:
            raise ValueError("No supported final output")
        sort = answer["dag_sort"]
        distinct = answer["dag_distinct"] == "yes"
        if distinct and sort != "none" and sort not in outputs:
            source = dag.window(
                source,
                "output_occurrence",
                op("row_number"),
                partition=outputs,
                order=[(sort, answer["dag_sort_direction"] == "desc")],
                description="Choose the first occurrence of each returned tuple in the requested order",
            )
            source = dag.filter(source, op("eq", ref("output_occurrence"), value(1)))
            distinct = False
        return dag.project(
            source,
            {f"result_{i + 1}": ref(k) for i, k in enumerate(outputs)},
            description="Return exactly the requested answer columns",
            distinct=distinct,
            order=[] if sort == "none" else [(sort, answer["dag_sort_direction"] == "desc")],
            limit=None if answer["dag_limit"] == "none" else int(answer["dag_limit"]),
        )
