"""Inspectable planning decisions and server-owned correction sessions."""

from copy import deepcopy
import time
import re
import os
from concurrent.futures import ThreadPoolExecutor, Future
from threading import Lock

from ..evaluators import ProviderError

from sqlalchemy import insert

from ..ledger import digest, now, uid
from .catalog import Catalog, serial
from . import schema
from .jev import affirmed, most_likely, selected


class PlanReviewRequired(ValueError):
    def __init__(
        self, plan, message="The proposed plan needs review. No query or mutation was executed."
    ):
        super().__init__(message)
        self.plan = plan


class DecisionUncertain(ValueError):
    def __init__(self, decision_id=None):
        super().__init__("Jev was uncertain among legal planning alternatives; clarify the request")
        self.decision_id = decision_id


def catalog_signature(datasets):
    return digest(
        serial(
            [
                {
                    key: (
                        [{k: v for k, v in c.items() if k != "values"} for c in dataset[key]]
                        if key == "columns"
                        else dataset[key]
                    )
                    for key in (
                        "id",
                        "name",
                        "description",
                        "columns",
                        "primary_key",
                        "links",
                        "writable",
                    )
                }
                for dataset in datasets
            ]
        )
    )


class ReviewDecisions:
    def __init__(self, delegate, previous=None, corrections=None):
        self.delegate, self.model = delegate, delegate.model
        self.previous = previous or {}
        self.corrections = corrections or {}
        self.batches, self.decisions = [], []
        self.applied = set()
        self.requests = 0
        self._cache_lock = Lock()
        self._inflight = {}
        self.prior_overrides = self.previous.get("overrides", {})
        available = {d["id"]: d for d in self.previous.get("decisions", [])}
        for identity, value in self.corrections.items():
            decision = available.get(identity)
            if not decision or not decision["editable"]:
                raise ValueError("Correction does not identify an editable decision in this review")
            if decision["kind"] == "choice":
                if not isinstance(value, str) or value not in {
                    o["id"] for o in decision["options"]
                }:
                    raise ValueError("Correction must select one of this decision's legal options")
            elif not isinstance(value, bool):
                raise ValueError("A Boolean decision requires true or false")

    def _evaluate(self, tenant, state, questions):
        signature = digest(serial([tenant, self.model, state, questions]))
        prior = next(
            (b for b in self.previous.get("batches", []) if b["signature"] == signature), None
        )
        if prior:
            return signature, deepcopy(prior["result"]), True
        with self._cache_lock:
            owner = signature not in self._inflight
            pending = self._inflight.setdefault(signature, Future())
            self.requests += int(owner)
        if owner:
            try:
                model_state = serial(
                    {key: value for key, value in state.items() if key != "option_sql"}
                )
                pending.set_result(deepcopy(self.delegate.ask(tenant, model_state, questions)))
            except BaseException as exc:
                pending.set_exception(exc)
                raise
        return signature, deepcopy(pending.result()), not owner

    def ask(self, tenant, state, questions):
        return self._record(state, questions, self._evaluate(tenant, state, questions))

    def ask_many(self, tenant, jobs, *, allow_partial=False):
        """Evaluate independent states concurrently and record in stable input order."""
        if not jobs:
            return []
        workers = max(1, min(16, int(os.getenv("SDD_PLANNING_WORKERS", "4"))))

        def record(state, questions, future):
            try:
                return self._record(state, questions, future.result())
            except ProviderError as exc:
                if not allow_partial:
                    raise
                return {
                    "answers": {},
                    "failure": {
                        "output_state": "NOT_EVALUATED",
                        "operation_state": "FAILED",
                        "code": exc.code,
                    },
                }

        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            pending = [
                pool.submit(self._evaluate, tenant, state, questions) for state, questions in jobs
            ]
            return [
                record(state, questions, future)
                for (state, questions), future in zip(jobs, pending)
            ]

    def _record(self, state, questions, evaluated):
        stage = len(self.batches)
        signature, result, reused = evaluated
        self.batches.append({"signature": signature, "result": deepcopy(result)})
        if reused:
            result["usage"] = {"input_tokens": 0, "output_tokens": 0}
        for key, question in questions.items():
            identity = f"{stage}.{key}." + digest([signature, serial(question)])[:16]
            answer = result["answers"][key]
            original = deepcopy(answer)
            editable = key != "complete" and not key.startswith("check_")
            forced = identity in self.corrections or identity in self.prior_overrides
            if forced and editable:
                value = self.corrections.get(identity, self.prior_overrides.get(identity))
                if question["type"] == "choice":
                    if value not in question["criteria"]:
                        raise ValueError("A corrected option is no longer a legal candidate")
                answer["_selection"] = value
                self.applied.add(identity)
            answer["_decision_id"] = identity
            answer["_proposal"] = True
            choice = question["type"] == "choice"
            model_selection = most_likely(original) if choice else original["noul"] >= 0.5
            probability = original["probabilities"][model_selection] if choice else original["noul"]
            self.decisions.append(
                {
                    "id": identity,
                    "stage": stage,
                    "key": key,
                    "field": state.get("decision_fields", {}).get(key)
                    or next(
                        (
                            field
                            for i, field in enumerate(state.get("fields", []))
                            if re.search(r"(?:^|_)[cf]" + str(i) + r"(?:_|$)", key)
                        ),
                        None,
                    ),
                    "kind": question["type"],
                    "instruction": question["instructions"],
                    "output_state": "UNKNOWN"
                    if not forced and (probability < 0.55 if choice else 0.2 < probability < 0.8)
                    else "VALUE",
                    "operation_state": "COMPLETED",
                    "editable": editable,
                    "selected": selected(answer) if choice else affirmed(answer),
                    "model_selected": model_selection,
                    "probability": probability,
                    "overridden": forced and editable,
                    "uncertain": probability < 0.55 if choice else 0.2 < probability < 0.8,
                    "options": [
                        {
                            "id": option,
                            "label": label,
                            "sql": state.get("option_sql", {}).get(key, {}).get(option),
                            "probability": original["probabilities"][option],
                        }
                        for option, label in question.get("criteria", {}).items()
                    ],
                }
            )
        return result

    def attach(self, plan, *, reason=None, failed_decision=None, detail=None):
        bindings = plan.pop("_plan_bindings", {})
        for decision in self.decisions:
            applied = bindings.get(decision["key"])
            options = {item["id"]: item for item in decision["options"]}
            if (
                applied in options
                and applied != decision["selected"]
                and not decision["overridden"]
            ):
                decision["model_probability"] = decision["probability"]
                decision["selected"] = applied
                decision["probability"] = options[applied]["probability"]
                decision["selected_by"] = "relational_search"
                decision["uncertain"] = decision["probability"] < 0.55
        unused = set(self.corrections) - self.applied
        unresolved = list(plan.pop("_unresolved", []))
        partial = bool(unresolved)
        uncertain = [
            decision
            for decision in self.decisions
            if decision["uncertain"]
            and not decision["overridden"]
            and decision["key"] != "complete"
        ]
        for decision in uncertain:
            unresolved.append(
                {
                    "code": "uncertain_decision",
                    "detail": "The most likely interpretation remains uncertain; inspect this decision.",
                    "decision_id": decision["id"],
                }
            )
        if unused:
            reason = "stale_correction"
            detail = "Earlier choices changed the available decisions. Review the rebuilt options."
        elif reason is None and partial:
            reason = "incomplete_plan"
            detail = (
                "This partial read proposal omits unresolved instructions. Review every omission."
            )
        elif reason is None and uncertain:
            reason = "decision_uncertain"
            failed_decision = uncertain[0]["id"]
            detail = "Uncertain decisions were used to draft SQL and need your confirmation."
        if reason and reason not in ("decision_uncertain", "incomplete_plan"):
            unresolved.append({"code": reason, "detail": detail, "decision_id": failed_decision})
        elif reason == "incomplete_plan" and not partial:
            unresolved.append(
                {"code": "incomplete_plan", "detail": detail, "decision_id": failed_decision}
            )
        has_sql = bool(plan.get("logical_sql"))
        plan["proposal"] = {
            "status": "partial"
            if has_sql and partial
            else "candidate"
            if has_sql
            else "unresolved",
            "strategy": "coherent_read_subset"
            if has_sql and partial
            else "typed_relational_candidates"
            if has_sql and plan.get("search")
            else "highest_probability"
            if has_sql
            else "no_legal_query",
        }
        plan["review"] = {
            "reason": reason,
            "failed_decision": failed_decision,
            "detail": detail,
            "decisions": self.decisions,
            "unresolved": unresolved,
            "requires_confirmation": bool(reason),
            "can_confirm_sql": has_sql
            and reason != "stale_correction"
            and (not partial or plan.get("operation") == "select"),
        }
        plan["_review_state"] = {
            "batches": self.batches,
            "decisions": self.decisions,
            "overrides": {d["id"]: d["selected"] for d in self.decisions if d["overridden"]},
        }
        plan["planning_requests"] = self.requests
        if unused:
            raise PlanReviewRequired(plan, detail)
        return plan


def public_plan(plan):
    return {key: value for key, value in plan.items() if not key.startswith("_")}


class PlanReviews:
    def __init__(self, db, planner):
        self.db, self.catalog, self.planner = db, Catalog(db), planner

    def save(self, tenant, actor, plan):
        identity = uid()
        stored = deepcopy(plan)
        stored["_actor"] = actor
        stored["_expires_at"] = time.time() + 900
        with self.db.transaction(tenant) as connection:
            connection.execute(
                insert(schema.runs).values(
                    id=identity,
                    tenant=tenant,
                    request=plan["request"],
                    logical_sql=plan.get("logical_sql", ""),
                    compiled_sql="",
                    parameters={},
                    plan=serial(stored),
                    manifest={
                        "operation": "planning_review",
                        "executed": False,
                        "dataset_ids": plan.get("_selected_dataset_ids", plan["dataset_ids"]),
                    },
                    result=[],
                    created_at=now(),
                )
            )
        result = public_plan(plan)
        result["review_id"] = identity
        return result

    def load(self, tenant, actor, identity, *, allow_expired=False):
        record = self.catalog.ledger.get(tenant, schema.runs, identity)
        plan = record["plan"]
        if record["manifest"].get("operation") != "planning_review" or plan.get("_actor") != actor:
            raise ValueError("This planning review is unavailable to this actor")
        if not allow_expired and plan.get("_expires_at", 0) < time.time():
            raise ValueError("This planning review expired; plan the request again")
        current = self.catalog.model_catalog(
            tenant, plan.get("_selected_dataset_ids", plan["dataset_ids"]), include_values=False
        )
        if catalog_signature(current) != plan["_catalog_signature"]:
            raise ValueError("Catalog definitions changed; plan the request again")
        return plan

    def reopen(self, tenant, actor, identity):
        plan = deepcopy(self.load(tenant, actor, identity, allow_expired=True))
        plan.pop("human_confirmation", None)
        plan["review"]["requires_confirmation"] = True
        return self.save(tenant, actor, plan)

    def begin(self, tenant, actor, question, dataset_ids=None, *, knowledge=None):
        try:
            plan = self.planner.plan(
                tenant, question, dataset_ids, **({"knowledge": knowledge} if knowledge else {})
            )
        except PlanReviewRequired as exc:
            raise PlanReviewRequired(self.save(tenant, actor, exc.plan), str(exc)) from exc
        return self.save(tenant, actor, plan)

    def resume(self, tenant, actor, identity, corrections=None):
        previous = self.load(tenant, actor, identity)
        try:
            plan = self.planner.plan(
                tenant,
                previous["request"],
                previous.get("_selected_dataset_ids", previous["dataset_ids"]),
                previous=previous,
                corrections=corrections,
            )
        except PlanReviewRequired as exc:
            raise PlanReviewRequired(self.save(tenant, actor, exc.plan), str(exc)) from exc
        plan["corrected_from"] = identity
        return self.save(tenant, actor, plan)

    def confirm(self, tenant, actor, identity):
        plan = deepcopy(self.load(tenant, actor, identity))
        if not plan.get("logical_sql") or not plan["review"].get("can_confirm_sql"):
            raise ValueError("Correct the incomplete plan before confirming SQL")
        plan["review"]["requires_confirmation"] = False
        plan["human_confirmation"] = {
            "actor": actor,
            "at": now(),
            "review_id": identity,
            "sql_hash": digest(plan["logical_sql"]),
        }
        return {**public_plan(plan), "review_id": identity}
