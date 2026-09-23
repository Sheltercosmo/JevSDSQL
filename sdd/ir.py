"""Bounded, validated query IR. No user-supplied SQL identifiers or fragments."""

from __future__ import annotations
from typing import Literal
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Predicate(Strict):
    op: Literal["true", "semantic", "eq", "and", "or", "not"]
    concept_id: str | None = None
    field: Literal["segment", "product", "customer_id"] | None = None
    value: str | None = None
    args: list[Predicate] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self):
        if self.op == "true":
            valid = (
                not self.args
                and self.field is None
                and self.value is None
                and self.concept_id is None
            )
        elif self.op == "semantic":
            valid = (
                bool(self.concept_id)
                and not self.args
                and self.field is None
                and self.value is None
            )
        elif self.op == "eq":
            valid = (
                self.field is not None
                and self.value is not None
                and not self.args
                and self.concept_id is None
            )
        else:
            valid = len(self.args) == 1 if self.op == "not" else len(self.args) >= 2
            valid = valid and self.concept_id is None and self.field is None and self.value is None
        if not valid:
            raise ValueError("Invalid predicate shape")
        return self

    def concepts(self):
        return ({self.concept_id} if self.op == "semantic" else set()).union(
            *(a.concepts() for a in self.args)
        )


class Plan(Strict):
    operation: Literal["list", "count", "group", "rank"] = "count"
    grain: Literal["message", "customer"] = "customer"
    quantifier: Literal["exists", "not_exists"] = "exists"
    predicate: Predicate
    scope: list[Predicate] = Field(default_factory=list, max_length=16)
    evaluator_id: str
    policy_id: str
    mode: Literal["complete", "explore"] = "complete"
    start: str | None = None
    end: str | None = None
    group_by: Literal["segment", "product"] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    candidate_limit: int = Field(default=100, ge=1, le=10000)
    max_evaluations: int = Field(default=100, ge=0, le=10000)
    wait_seconds: float = Field(default=30, ge=0, le=300)

    @model_validator(mode="after")
    def validate_plan(self):
        if any(p.op != "eq" for p in self.scope):
            raise ValueError("Population scope accepts equality filters only")
        if self.quantifier == "not_exists" and (
            self.grain != "customer" or self.operation in ("group", "rank")
        ):
            raise ValueError("not_exists supports customer count/list only")
        if (self.operation in ("group", "rank")) != (self.group_by is not None):
            raise ValueError("group/rank require group_by; other operations must omit it")
        for name in ("start", "end"):
            value = getattr(self, name)
            if value:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError("Date boundaries must include a UTC offset")
                setattr(self, name, dt.astimezone(timezone.utc).isoformat())
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must precede end (exclusive)")

        def size(p):
            return 1 + sum(size(a) for a in p.args)

        if size(self.predicate) > 64:
            raise ValueError("Predicate exceeds 64 nodes")
        return self
