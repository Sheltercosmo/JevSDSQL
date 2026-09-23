"""Bounded Jev decisions shared by generic planning and row classification."""

from email.utils import parsedate_to_datetime
import math
import os
from threading import BoundedSemaphore, Lock
from contextlib import contextmanager
import httpx
from datetime import datetime, timezone
from sqlalchemy import update
from .. import schema as base
from ..ledger import digest
from ..evaluators import ProviderError


def reserve(db, tenant):
    day = str(datetime.now(timezone.utc).date())
    identity = digest([tenant, day])
    with db.transaction(tenant) as connection:
        insert = __import__(
            "sqlalchemy.dialects." + db.engine.dialect.name, fromlist=["insert"]
        ).insert
        connection.execute(
            insert(base.usage_budgets)
            .values(id=identity, tenant=tenant, day=day, calls=0)
            .on_conflict_do_nothing()
        )
        result = connection.execute(
            update(base.usage_budgets)
            .where(
                base.usage_budgets.c.id == identity,
                base.usage_budgets.c.tenant == tenant,
                base.usage_budgets.c.calls < int(os.getenv("SDD_DAILY_EVALUATIONS", "1000")),
            )
            .values(calls=base.usage_budgets.c.calls + 1)
        )
        if not result.rowcount:
            raise ProviderError("DailyBudgetExceeded", True)


_limiter_lock = Lock()
_tenant_limiters = {}
_global_slots = BoundedSemaphore(16)


def finite_number(value, minimum, maximum):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def validate_response(result, questions, model):
    try:
        usage = result.get("usage", {})
        if not isinstance(usage, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in usage.values()
        ):
            raise ValueError("Invalid usage")
        if result["model"] != model or set(result["answers"]) != set(questions):
            raise ValueError("Wrong model or answer keys")
        for key, question in questions.items():
            answer = result["answers"][key]
            if answer["type"] != question["type"]:
                raise ValueError("Wrong primitive")
            if question["type"] == "noul":
                if not finite_number(answer["noul"], 0, 1):
                    raise ValueError("Invalid probability")
                continue
            probabilities = answer["probabilities"]
            expected = (
                set(question["criteria"])
                if question["type"] == "choice"
                else {str(i) for i in range(len(question["criteria"]))}
            )
            if set(probabilities) != expected:
                raise ValueError("Invalid candidates")
            if any(not finite_number(value, 0, 1) for value in probabilities.values()):
                raise ValueError("Invalid distribution")
            if abs(sum(probabilities.values()) - 1) > 0.03:
                raise ValueError("Unnormalized distribution")
            if "confidence" in answer and not finite_number(answer["confidence"], 0, 1):
                raise ValueError("Invalid confidence")
            if question["type"] == "choice":
                if answer["choice"] not in expected:
                    raise ValueError("Unknown choice")
            else:
                if not finite_number(answer["score"], 0, len(expected) - 1):
                    raise ValueError("Invalid score")
                if answer["legend"] != {
                    str(i): value for i, value in enumerate(question["criteria"])
                }:
                    raise ValueError("Score rubric changed")
        return result
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ProviderError("InvalidDecisionResponse", False) from exc


class Decisions:
    def __init__(self, db, backend, model="jev-1.13.0"):
        self.db, self.backend, self.model = db, backend, model
        if "latest" in model or "preview" in model:
            raise ValueError("Pin the Jev model version")
        self.concurrency = max(1, min(16, int(os.getenv("SDD_JEV_CONCURRENCY", "4"))))

    @contextmanager
    def slot(self, tenant):
        key = (id(self.db.engine), tenant, self.concurrency)
        with _limiter_lock:
            slots = _tenant_limiters.setdefault(key, BoundedSemaphore(self.concurrency))
        with slots, _global_slots:
            yield

    def ask(self, tenant, state, questions):
        if not questions or len(questions) > 128:
            raise ValueError("A decision batch requires 1–128 questions")
        for question in questions.values():
            kind = question["type"]
            if kind not in ("noul", "choice", "score"):
                raise ValueError("Unsupported Jev primitive")
            if kind == "choice" and not 2 <= len(question["criteria"]) <= 255:
                raise ValueError("Choice candidates must have 2–255 options")
            if kind == "score" and not 2 <= len(question["criteria"]) <= 10:
                raise ValueError("Score requires 2–10 ordered levels")
        with self.slot(tenant):
            reserve(self.db, tenant)
            try:
                response = self.backend.client.post(
                    "https://api.typesafe.ai/v1/systemone",
                    headers={"Authorization": "Bearer " + self.backend.api_key},
                    json={"model": self.model, "state": state, "questions": questions},
                )
            except httpx.RequestError as exc:
                raise ProviderError(type(exc).__name__, True) from exc
            if not response.is_success:
                retry_after = None
                header = response.headers.get("Retry-After")
                if header:
                    try:
                        retry_after = float(header)
                    except ValueError:
                        try:
                            retry_after = (
                                parsedate_to_datetime(header) - datetime.now(timezone.utc)
                            ).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            pass
                    if retry_after is not None and (
                        not math.isfinite(retry_after) or retry_after < 0
                    ):
                        retry_after = None
                raise ProviderError(
                    "HTTP_" + str(response.status_code),
                    response.status_code in (408, 429) or response.status_code >= 500,
                    retry_after=retry_after,
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise ProviderError("InvalidDecisionResponse", False) from exc
            return validate_response(result, questions, self.model)


def choice(instructions, options):
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {str(k): v for k, v in options.items()},
    }


def noul(instructions):
    return {"type": "noul", "instructions": instructions}


def most_likely(answer):
    probabilities = answer["probabilities"]
    best = max(probabilities, key=probabilities.get)
    original = answer["choice"]
    return original if probabilities[original] == probabilities[best] else best


def selected(answer, threshold=0.55):
    if "_selection" in answer:
        return answer["_selection"]
    key = most_likely(answer) if answer.get("_proposal") else answer["choice"]
    score = answer["probabilities"][key]
    if score < threshold and not answer.get("_proposal"):
        from .planning_review import DecisionUncertain

        raise DecisionUncertain(answer.get("_decision_id"))
    return key


def affirmed(answer, threshold=0.8):
    if "_selection" in answer:
        return answer["_selection"]
    return answer["noul"] >= (0.5 if answer.get("_proposal") else threshold)
