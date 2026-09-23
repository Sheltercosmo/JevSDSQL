"""Value knowledge and execution status are independent dimensions."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class OutputState(StrEnum):
    VALUE = "VALUE"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


class OperationState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    BLOCKED_BY_BUDGET = "BLOCKED_BY_BUDGET"
    BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    TRUNCATED = "TRUNCATED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


@dataclass(frozen=True)
class Decision:
    output_state: OutputState
    operation_state: OperationState = OperationState.SUCCEEDED
    value: Any = None
    raw: dict | None = None
    reason: str | None = None
    observation_id: str | None = None
    cached: bool = False
    dependencies: tuple[str, ...] = ()

    def __post_init__(self):
        if self.output_state != OutputState.VALUE and self.value is not None:
            raise ValueError("Only VALUE may contain a resolved value")
        if self.output_state == OutputState.VALUE and self.value is None:
            raise ValueError("VALUE requires a value; use UNKNOWN for a missing value")

    def __bool__(self):
        raise TypeError("Inspect output_state and value explicitly; missing is not false")

    def json(self):
        return asdict(self)

    @classmethod
    def known(cls, value, **metadata):
        return cls(OutputState.VALUE, value=value, **metadata)

    @classmethod
    def unknown(cls, reason, **metadata):
        return cls(OutputState.UNKNOWN, reason=reason, **metadata)

    @classmethod
    def unexecuted(cls, reason, state=OperationState.SKIPPED, **metadata):
        return cls(OutputState.NOT_EVALUATED, state, reason=reason, **metadata)


@dataclass(frozen=True)
class Policy:
    revision: str = "unvalidated-default-v1"
    accept: float = 0.8
    reject: float = 0.2
    choice_min: float = 0.55
    score_confidence_min: float = 0.0
    unknown_options: tuple[str, ...] = ("unknown", "insufficient", "none", "other")

    def __post_init__(self):
        if not 0 <= self.reject < self.accept <= 1:
            raise ValueError("Policy requires 0 <= reject < accept <= 1")
        if not 0 <= self.choice_min <= 1 or not 0 <= self.score_confidence_min <= 1:
            raise ValueError("Confidence thresholds must be in [0,1]")

    def resolve(self, answer, **metadata):
        metadata = {"raw": answer, **metadata}
        if answer["type"] == "noul":
            probability = answer["noul"]
            if probability >= self.accept:
                return Decision.known(True, **metadata)
            if probability <= self.reject:
                return Decision.known(False, **metadata)
            return Decision.unknown(
                "Probability is inside the policy's abstention interval", **metadata
            )
        if answer["type"] == "choice":
            choice = answer["choice"]
            if choice in self.unknown_options or answer["probabilities"][choice] < self.choice_min:
                return Decision.unknown("No sufficiently supported in-scope option", **metadata)
            return Decision.known(choice, **metadata)
        if answer.get("confidence", 0) < self.score_confidence_min:
            return Decision.unknown("Rating confidence is below the declared policy", **metadata)
        return Decision.known(answer["score"], **metadata)


def logical(operator, values):
    if operator not in {"and", "or", "not"} or not values:
        raise ValueError("Expected and/or/not with operands")
    if operator == "not" and len(values) != 1:
        raise ValueError("NOT requires one operand")
    known = [d.value for d in values if d.output_state == OutputState.VALUE]
    if any(type(value) is not bool for value in known):
        raise ValueError("Logical operands must be Boolean values")
    if operator == "and" and False in known:
        return Decision.known(False)
    if operator == "or" and True in known:
        return Decision.known(True)
    missing = [d for d in values if d.output_state == OutputState.NOT_EVALUATED]
    if missing:
        return Decision.unexecuted(
            "A required logical operand was not evaluated", OperationState.BLOCKED_BY_DEPENDENCY
        )
    if any(d.output_state == OutputState.UNKNOWN for d in values):
        return Decision.unknown("A required logical operand is unknown")
    if operator == "not":
        return Decision.known(not known[0])
    if operator == "and":
        return Decision.known(all(known))
    return Decision.known(any(known))


@dataclass(frozen=True)
class WorkItem:
    id: str
    state: Any
    question: dict
    subject_id: str = ""
    source_revisions: tuple[str, ...] = ()
    hypothesis: dict = field(default_factory=dict)


def coverage(decisions, eligible=None):
    decisions = list(decisions)
    counts = {state.value: sum(d.output_state == state for d in decisions) for state in OutputState}
    eligible = len(decisions) if eligible is None else eligible
    return {
        "eligible": eligible,
        "accounted": len(decisions),
        "evaluated": sum(d.raw is not None for d in decisions),
        "decided": counts["VALUE"],
        "unknown": counts["UNKNOWN"],
        "not_evaluated": counts["NOT_EVALUATED"] + max(0, eligible - len(decisions)),
        "output_states": counts,
        "operation_states": {
            state.value: sum(d.operation_state == state for d in decisions)
            for state in OperationState
        },
        "complete": eligible == len(decisions)
        and all(d.output_state == OutputState.VALUE for d in decisions),
    }


def result_states(decisions, complete, truncated=False):
    decisions = list(decisions)
    operations = {decision.operation_state for decision in decisions}
    operation = OperationState.TRUNCATED if truncated else OperationState.SUCCEEDED
    for candidate in (
        OperationState.CANCELLED,
        OperationState.STALE,
        OperationState.FAILED,
        OperationState.BLOCKED_BY_BUDGET,
        OperationState.BLOCKED_BY_POLICY,
        OperationState.BLOCKED_BY_DEPENDENCY,
    ):
        if candidate in operations:
            operation = candidate
            break
    if complete:
        return OutputState.VALUE, operation
    if decisions and all(
        decision.output_state == OutputState.NOT_EVALUATED for decision in decisions
    ):
        return OutputState.NOT_EVALUATED, operation
    return OutputState.UNKNOWN, operation
