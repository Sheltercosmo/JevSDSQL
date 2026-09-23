"""Authorized catalog adapter and public dispatch for the proposed JEV interfaces."""

import inspect
import json
import time
from dataclasses import asdict
from importlib.resources import files

from sqlalchemy import select, text, update

from ..generic import schema as catalog_schema
from ..generic.catalog import Catalog, serial
from ..generic.planner import Planner
from ..generic.planning_review import PlanReviewRequired
from ..ledger import digest
from . import schema
from .advanced import AdvancedOperators
from .extraction import ExtractionOperators
from .budget import Limits, estimate, expansion
from .core import CoreOperators, question
from .relations import RelationalOperators
from .runtime import Runtime
from .store import Store
from .types import (
    Decision,
    Policy,
    WorkItem,
    result_states,
)
from .types import (
    OperationState as Op,
)
from .types import (
    OutputState as Out,
)
from .workflows import WorkflowOperators


def manifest():
    return json.loads(files("sdd.operators").joinpath("manifest.json").read_text(encoding="utf-8"))


def operator_options(name, limits=None, policy=None):
    extraction = name.upper().removeprefix("JEV.") == "EXTRACT_TABLE"
    return (
        Limits(**({"max_stages": 4, **(limits or {})} if extraction else (limits or {}))),
        Policy(**({"choice_min": 0.8, **(policy or {})} if extraction else (policy or {}))),
    )


class AdmissionBlocked(ValueError):
    pass


class OperatorService(
    CoreOperators, RelationalOperators, WorkflowOperators, AdvancedOperators, ExtractionOperators
):
    def __init__(self, db, decisions, tenant, actor, role="reader"):
        self.db, self.decisions, self.tenant, self.actor, self.role = (
            db,
            decisions,
            tenant,
            actor,
            role,
        )
        self.catalog = Catalog(db)
        self.store = Store(db, tenant, actor)
        self.snapshots = []
        self.scope_incomplete = False

    def call(self, operator, arguments=None, limits=None, policy=None, approval_id=None):
        """Run an operator with a fresh budget and return its states, value and evidence.

        Use this entry point; individual operator methods share its execution context.
        API request examples are in docs/JEV_FUNCTION_REFERENCE.md.
        """
        name = operator.upper().removeprefix("JEV.")
        arguments = serial(arguments or {})
        method = self._validated_method(name, arguments)
        limits, policy = operator_options(name, limits, policy)
        self.snapshots = []
        self.scope_incomplete = False
        self.expected_sources = self.source_signature(arguments, name)
        request = {
            "source_signature": self.expected_sources,
            "operator": name,
            "arguments": arguments,
            "limits": asdict(limits),
            "policy": asdict(policy),
        }
        approved = self._check_approval(approval_id, request)
        run_id = self.store.start("JEV." + name, request)
        self.runtime = Runtime(
            self.store, self.decisions, run_id, limits, policy, approved, self.fresh
        )
        result = self._invoke(method, arguments)
        output = serial(self._publish_result(name, run_id, result))
        self.store.finish(run_id, output)
        return output

    def _validated_method(self, name, arguments):
        allowed = {f["name"].removeprefix("JEV.") for f in manifest()["functions"]}
        if name not in allowed | {"WORKFLOW"}:
            raise ValueError("Unknown JEV operator")
        method = getattr(self, name.lower())
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        return method

    def _check_approval(self, approval_id, request):
        if not approval_id:
            return False
        approval = self.store.get(schema.approvals, approval_id)
        if approval["plan_hash"] != digest(request) or approval["expires_at"] <= time.time():
            raise ValueError("Approval does not cover this exact request and budget or has expired")
        return True

    def _invoke(self, method, arguments):
        try:
            return method(**arguments)
        except AdmissionBlocked as exc:
            decision = Decision.unexecuted(str(exc), Op.BLOCKED_BY_BUDGET)
            return self.result(None, {"admission": decision})
        except Exception:
            self.store.finish(
                self.runtime.run_id,
                {
                    "operation_state": "FAILED",
                    "output_state": "NOT_EVALUATED",
                    "value": None,
                    "reason": "Operator validation or execution failed",
                    "manifest": self.runtime.manifest([]),
                },
            )
            raise

    def _publish_result(self, name, run_id, result):
        observations = result.pop("decisions", {})
        stats = self.runtime.manifest(observations.values())
        complete = result.pop("answer_complete", None)
        if complete is None:
            complete = result.get("complete", True) and stats["complete"]
        complete = (
            complete
            and not self.scope_incomplete
            and result.get("scope_complete", True)
            and not result.get("truncated", False)
        )
        if not self.fresh():
            complete = False
            observations = {
                k: Decision.unexecuted("Source changed before result publication", Op.STALE)
                for k in observations
            }
        if self.runtime.cancelled():
            complete = False
            observations = {
                k: Decision.unexecuted("Job cancelled", Op.CANCELLED) for k in observations
            } or {"run": Decision.unexecuted("Job cancelled", Op.CANCELLED)}
        stats = self.runtime.manifest(observations.values())
        output_state, operation = result_states(
            observations.values(), complete, result.get("truncated", False)
        )
        output = {
            "operator": "JEV." + name,
            "run_id": run_id,
            "output_state": output_state,
            "operation_state": operation,
            "value": result.get("value") if complete else None,
            "observations": {k: d.json() for k, d in observations.items()},
            "manifest": {
                **stats,
                "answer_complete": bool(complete),
                "scope_complete": not self.scope_incomplete and result.get("scope_complete", True),
                "source_snapshots": [s[2] for s in self.snapshots],
            },
            **{k: v for k, v in result.items() if k not in {"value", "complete"}},
        }
        if not complete:
            output["partial_value"] = result.get("value")
        return output

    def result(self, value, decisions=None, **metadata):
        return {"value": value, "decisions": decisions or {}, **metadata}

    @staticmethod
    def fingerprint(value):
        return digest(serial(value))

    @staticmethod
    def positive_int(value, name):
        if type(value) is not int or value < 1:
            raise ValueError(name + " must be a positive integer")

    @staticmethod
    def text(subject):
        value = subject["state"]
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("text", json.dumps(value, ensure_ascii=False)))
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def field(subject, key):
        return subject["state"].get(key) if isinstance(subject["state"], dict) else None

    def definition(self, reference):
        if isinstance(reference, dict) and "revision_id" in reference:
            return self.store.get(schema.definitions, reference["revision_id"])["definition"]
        if isinstance(reference, str):
            try:
                return self.store.get(schema.definitions, reference)["definition"]
            except ValueError:
                return {"instructions": reference}
        if not isinstance(reference, dict):
            raise ValueError(
                "Definition requires a revision ID, instructions or structured definition"
            )
        return reference

    def subjects(self, source):
        if isinstance(source, dict) and "dataset_id" in source:
            dataset = self.catalog.get(self.tenant, source["dataset_id"])
            if not dataset["primary_key"]:
                raise ValueError("Operator datasets require stable primary keys")
            rows = self.catalog.rows(self.tenant, dataset, limit=5001)
            if len(rows) > 5000:
                raise AdmissionBlocked(
                    "Registered operator scope exceeds 5,000 rows; provide an explicit smaller registered population"
                )
            snapshot = self.population_hash(rows)
            self.snapshots.append((dataset, source, snapshot))
            rows = [
                row
                for row in rows
                if all(row.get(k) == v for k, v in source.get("where", {}).items())
            ]
            return [
                {
                    "id": digest(serial([row[k] for k in dataset["primary_key"]])),
                    "state": serial(row),
                    "column_types": {c["name"]: c["type"] for c in dataset["columns"]},
                    "revisions": [dataset["id"], digest(serial(row))],
                }
                for row in rows
            ]
        if not isinstance(source, list):
            source = [source]
        if len(source) > 5000:
            raise AdmissionBlocked("Explicit operator population exceeds 5,000 subjects")
        output = []
        for i, item in enumerate(source):
            if isinstance(item, dict) and set(("id", "state", "revisions")) <= set(item):
                if (
                    not isinstance(item["id"], str)
                    or not item["id"]
                    or not isinstance(item["revisions"], list)
                    or any(not isinstance(r, str) for r in item["revisions"])
                ):
                    raise ValueError("Normalized subjects require string IDs and revision lists")
                output.append(item)
                continue
            identity = str(item.get("id", i)) if isinstance(item, dict) else str(i)
            state = item
            output.append({"id": identity, "state": state, "revisions": [digest(serial(state))]})
        if len({s["id"] for s in output}) != len(output):
            raise ValueError("Subject IDs must be unique within the declared population")
        return output

    @staticmethod
    def population_hash(rows):
        return digest(sorted(digest(serial(row)) for row in rows))

    def source_signature(self, arguments, operator):
        name = operator.upper().removeprefix("JEV.")
        paths = {
            "PROMPT": ["subjects"],
            "CLASSIFY": ["subjects"],
            "TAG": ["subjects"],
            "FILTER": ["relation"],
            "RANK": ["subjects"],
            "RERANK": ["candidates"],
            "COMPOSITE_SCORE": ["subjects"],
            "EXTRACT": ["subjects"],
            "EXTRACT_DATE": ["subjects"],
            "FIND": ["corpus"],
            "SUMMARY_EXTRACTIVE": ["subjects"],
            "JOIN": ["left", "right"],
            "ALIGN": ["left", "right"],
            "VERIFY": ["evidence"],
            "RELATE": ["left", "right"],
            "COVER": ["obligations", "evidence_scope"],
            "AGGREGATE": ["relation"],
            "CONTRAST": ["left", "right"],
            "EVIDENCE_JOIN": ["claims", "candidate_sources"],
            "STATE_SCAN": ["blocks"],
            "TRACE": ["records"],
            "MATCH": ["records"],
            "RESOLVE": ["plan.subjects"],
            "DISCOVER": ["records", "discovery_budget.holdout"],
            "ENSURE_SEMANTICS": ["subject_revs"],
            "MATERIALIZE": ["target_scope"],
            "CLASSIFY_HIERARCHY": ["subjects"],
        }
        sources = []
        for path in paths.get(name, []):
            value = arguments
            for key in path.split("."):
                value = value.get(key) if isinstance(value, dict) else None
            sources.append(value)
        if name == "REFRESH":
            sources.extend(
                self.store.get(schema.generations, identity)["target_scope"]
                for identity in arguments.get("policies", [])
            )
        references = {}
        for value in sources:
            if isinstance(value, dict) and "dataset_id" in value:
                dataset = self.catalog.get(self.tenant, value["dataset_id"])
                rows = self.catalog.rows(self.tenant, dataset, limit=5001)
                references[dataset["id"]] = digest([serial(dataset), self.population_hash(rows)])
        return references

    def fresh(self, connection=None):
        if not self.snapshots:
            return True
        if connection is None:
            with self.db.transaction(self.tenant) as connection:
                return self.fresh(connection)
        for dataset, _, snapshot in self.snapshots:
            current = (
                connection.execute(
                    select(catalog_schema.datasets).where(
                        catalog_schema.datasets.c.tenant == self.tenant,
                        catalog_schema.datasets.c.id == dataset["id"],
                    )
                )
                .mappings()
                .first()
            )
            if current is None or digest(serial(dict(current))) != digest(serial(dataset)):
                return False
            if self.expected_sources.get(
                dataset["id"], digest([serial(dataset), snapshot])
            ) != digest([serial(dataset), snapshot]):
                return False
            table = self.catalog.table(dataset, connection)
            if connection.dialect.name == "postgresql":
                name = connection.dialect.identifier_preparer.format_table(table)
                connection.execute(text("LOCK TABLE " + name + " IN SHARE MODE"))
            rows = [dict(row) for row in connection.execute(select(table).limit(5001)).mappings()]
            if self.population_hash(rows) != snapshot:
                return False
        return True

    def admit_expansion(self, count):
        hard = self.runtime.budget.hard_work
        if count >= hard or count > 100000:
            raise AdmissionBlocked(
                f"W03/W06: {count} candidate judgments exceed the structural work cap"
            )
        if count >= 10000 and not self.runtime.approved:
            raise AdmissionBlocked(f"W02: {count} judgments require a plan-bound approval")

    def evaluate(self, work_items, limits=None, retry_policy=None, persist=True):
        """Submit explicit typed work items to the shared runtime."""
        if persist is not True:
            raise ValueError(
                "Database operator execution retains an audit record; nonpersistent execution is unsupported"
            )
        if limits is not None or retry_policy is not None:
            raise ValueError("Set limits and retries in the outer execution envelope")
        items = [
            WorkItem(
                str(w["id"]),
                w["state"],
                w["question"],
                str(w.get("subject_id", w["id"])),
                tuple(w.get("source_revisions", [])),
                w.get("hypothesis", {}),
            )
            for w in work_items
        ]
        decisions = self.runtime.evaluate(items)
        return self.result({k: d.json() for k, d in decisions.items()}, decisions)

    def ensure_semantics(self, subject_revs, concept_revs, evaluator_revs=None, budget=None):
        """Fill or reuse semantic observations for subject and concept revisions."""
        if evaluator_revs and set(evaluator_revs) != {self.runtime.model}:
            raise ValueError("Evaluator revisions must match the pinned configured model")
        if budget is not None:
            raise ValueError("Set budget in the outer limits envelope")
        return self.tag(subject_revs, concept_revs)

    def explain_plan(self, typed_plan, source_stats=None, limits=None, cache_stats=None):
        """Estimate missing semantic work before inference."""
        stats = source_stats or {}
        cache = cache_stats or {}
        operator = typed_plan["operator"].upper().removeprefix("JEV.")
        work = expansion(
            operator,
            stats.get("left", stats.get("subjects", 0)),
            stats.get("right", 0),
            stats.get("questions", 1),
            typed_plan.get("max_bundle_size", 2),
            stats.get("states", 1),
        )
        cached = cache.get("compatible_judgments", 0)
        if type(cached) is not int or not 0 <= cached <= work:
            raise ValueError("Compatible cache count is outside the planned work")
        fresh = work - cached
        requests = fresh
        tokens = stats.get(
            "input_token_upper_bound", fresh * stats.get("tokens_per_judgment", 1200)
        )
        if not fresh:
            tokens = 0
        details = estimate(
            fresh,
            requests,
            tokens,
            Limits(**limits) if limits else self.runtime.budget.limits,
            typed_plan.get("stages", 1),
        )
        details.update(
            total_judgments=work,
            compatible_cached=cached,
            estimate_only=True,
            request_accounting="Upper bound: one request per fresh judgment; runtime fuses compatible contexts",
            plan_hash=digest([typed_plan, stats, limits, cache]),
        )
        return self.result(details)

    def select_schema(self, request, authorized_catalog=None, candidate_policy=None):
        """Select relevant authorized tables and fields."""
        if candidate_policy is not None:
            raise ValueError(
                "Set an explicit authorized_catalog scope; heuristic schema pruning is unsupported"
            )
        datasets = self.catalog.list(self.tenant)
        if authorized_catalog is not None:
            permitted = {self.catalog.get(self.tenant, i)["id"] for i in authorized_catalog}
            datasets = [d for d in datasets if d["id"] in permitted]
        if len(datasets) > 20 or sum(len(d["columns"]) for d in datasets) > 150:
            raise AdmissionBlocked(
                "Schema pilot supports at most 20 tables / 150 columns; choose an explicit authorized scope"
            )
        subjects = [
            {
                "id": d["id"],
                "state": {
                    "request": request,
                    "table": d["name"],
                    "description": d["description"],
                    "columns": d["columns"],
                    "links": d["links"],
                },
                "revisions": [digest(serial(d))],
            }
            for d in datasets
        ]
        work = []
        for subject, dataset in zip(subjects, datasets):
            questions = {
                "needed": question(
                    "noul",
                    "Does this authorized table supply required operands or a necessary relationship bridge for the request?",
                )
            }
            for index, column in enumerate(dataset["columns"]):
                questions["column:" + str(index)] = question(
                    "noul",
                    {
                        "question": "Is this column needed to answer the request, filter, group, calculate or join at the intended grain?",
                        "column": column,
                    },
                )
            for key, q in questions.items():
                work.append(
                    WorkItem(
                        subject["id"] + ":" + key,
                        subject["state"],
                        q,
                        subject["id"],
                        tuple(subject["revisions"]),
                    )
                )
        decisions = self.runtime.evaluate(work)
        chosen = []
        for dataset in datasets:
            needed = decisions[dataset["id"] + ":needed"]
            fields = [
                column
                for i, column in enumerate(dataset["columns"])
                if decisions[dataset["id"] + ":column:" + str(i)].output_state == Out.VALUE
                and decisions[dataset["id"] + ":column:" + str(i)].value is True
            ]
            if needed.output_state == Out.VALUE and needed.value is True or fields:
                chosen.append(
                    {
                        "id": dataset["id"],
                        "name": dataset["name"],
                        "columns": fields,
                        "approved_links": dataset["links"],
                        "primary_key": dataset["primary_key"],
                    }
                )
        return self.result(
            {
                "tables": chosen,
                "alternatives": [{"id": d["id"], "name": d["name"]} for d in datasets],
                "relationships_invented": False,
                "catalog_scope": "Authorized tables only; values and business meaning still require plan validation",
            },
            decisions,
        )

    def plan_sql(self, request, catalog_rev=None, grammar_rev="staged-v1", mode="proposal"):
        """Construct a staged SQL proposal for a natural-language request."""
        if grammar_rev != "staged-v1" or mode != "proposal":
            raise ValueError(
                "PLAN_SQL supports staged-v1 proposals; execution requires the normal review endpoint"
            )
        if self.decisions is None:
            return self.result(
                None, {"plan": Decision.unexecuted("Provider is not configured", Op.FAILED)}
            )
        service = self

        class Adapter:
            model = service.runtime.model

            def ask(self, tenant, state, questions):
                work = [WorkItem(k, state, q, "sql-planning") for k, q in questions.items()]
                results = service.runtime.evaluate(work)
                if any(d.raw is None for d in results.values()):
                    raise AdmissionBlocked("SQL planning stopped at its reserved budget")
                return {
                    "model": self.model,
                    "answers": {k: d.raw for k, d in results.items()},
                    "usage": {},
                }

        try:
            plan = Planner(self.db, Adapter(), "staged").plan(self.tenant, request, catalog_rev)
        except PlanReviewRequired as exc:
            plan = exc.plan
        return self.result({"plan": plan, "executed": False}, answer_complete=True)

    def review(self, observations, sampling_policy=None, reviewer_role=None):
        """Save a human assertion without overwriting the model evidence."""
        self.require_reviewer()
        if sampling_policy and not observations:
            raise ValueError("Supply an explicit reviewed observation sample and corrections")
        output = []
        for item in observations:
            self.store.get(schema.observations, item["observation_id"])
            if not item.get("reason") or "value" not in item:
                raise ValueError("Human assertion requires value and reason")
            output.append(
                self.store.add(
                    schema.assertions,
                    observation_id=item["observation_id"],
                    actor=self.actor,
                    value=item["value"],
                    reason=item["reason"],
                )
            )
        return self.result({"assertions": output, "raw_observations_unchanged": True})

    def promote(self, candidate_rev, owner, validation_report, retention_policy):
        """Approve a provisional concept using independently reviewed evidence."""
        self.require_reviewer()
        candidate = self.store.get(schema.definitions, candidate_rev)
        if (
            not owner
            or not retention_policy
            or not validation_report.get("independent_holdout")
            or not validation_report.get("reviewed_examples")
        ):
            raise ValueError(
                "Promotion requires an owner, retention policy and independent reviewed holdout evidence"
            )
        approved = self.store.add(
            schema.definitions,
            name=candidate["name"],
            owner=owner,
            definition=candidate["definition"],
            status="ACTIVE",
            validation={
                **validation_report,
                "approved_by": self.actor,
                "candidate_revision": candidate_rev,
                "retention_policy": retention_policy,
            },
        )
        return self.result({"approved_revision": approved, "backfill_started": False})

    def materialize(self, concept_rev, target_scope, refresh_policy, budget=None):
        """Publish a generation for an approved concept and registered population."""
        self.require_reviewer()
        if budget is not None:
            raise ValueError("Set budget in the outer limits envelope")
        definition = self.store.get(schema.definitions, concept_rev)
        if definition["status"] != "ACTIVE":
            raise ValueError("Only approved concept revisions can be materialized")
        if not isinstance(target_scope, dict) or "dataset_id" not in target_scope:
            raise ValueError("Materialization requires a registered dataset scope")
        if refresh_policy.get("mode") not in {"explicit", "on_change"}:
            raise ValueError("Refresh policy must be explicit or on_change")
        if refresh_policy["mode"] == "on_change":
            for key in ("max_refreshes", "interval_seconds"):
                self.positive_int(refresh_policy.get(key), key)
            if refresh_policy["max_refreshes"] > 100 or refresh_policy["interval_seconds"] < 30:
                raise ValueError(
                    "Subscriptions require at most 100 refreshes and at least 30 seconds between checks"
                )
        result = self.tag(target_scope, [definition["definition"]])
        with self.db.transaction(self.tenant) as connection:
            complete = all(
                d.output_state == Out.VALUE for d in result["decisions"].values()
            ) and self.fresh(connection)
            generation = self.store.add(
                schema.generations,
                connection=connection,
                definition_id=concept_rev,
                run_id=self.runtime.run_id,
                target_scope=target_scope,
                refresh_policy=refresh_policy,
                snapshot=list(dict.fromkeys(s[2] for s in self.snapshots)),
                state="PUBLISHED" if complete else "PARTIAL",
                coverage=self.runtime.manifest(result["decisions"].values()),
            )
            if refresh_policy["mode"] == "on_change":
                self.store.add(
                    schema.subscriptions,
                    connection=connection,
                    owner=self.actor,
                    generation_id=generation["id"],
                    limits=asdict(self.runtime.budget.limits),
                    policy=asdict(self.runtime.policy),
                    remaining=refresh_policy["max_refreshes"],
                    interval_seconds=refresh_policy["interval_seconds"],
                    next_check=time.time() + refresh_policy["interval_seconds"],
                    lease_until=0,
                    last_state="ACTIVE",
                )
        result["value"] = {
            "generation": generation,
            "published": complete,
            "observations": result["value"],
        }
        return result

    def refresh(self, change_set, policies=None, budget=None):
        """Invalidate specified source dependencies and rebuild selected generations."""
        self.require_reviewer()
        if budget is not None:
            raise ValueError("Set budget in the outer limits envelope")
        invalid = self.store.invalidate(
            change_set.get("source_revisions", []), change_set.get("reason", "Dependency changed")
        )
        generations = []
        decisions = {}
        for identity in policies or []:
            previous = self.store.get(schema.generations, identity)
            with self.db.transaction(self.tenant) as connection:
                connection.execute(
                    update(schema.generations)
                    .where(
                        schema.generations.c.id == identity,
                        schema.generations.c.tenant == self.tenant,
                    )
                    .values(state="STALE")
                )
            result = self.materialize(
                previous["definition_id"], previous["target_scope"], previous["refresh_policy"]
            )
            generations.append(result["value"])
            decisions.update({identity + ":" + k: d for k, d in result["decisions"].items()})
        return self.result(
            {"invalidated_observations": invalid, "replacement_generations": generations}, decisions
        )

    def require_reviewer(self):
        if self.role != "reviewer":
            raise PermissionError("Reviewer role is required")
