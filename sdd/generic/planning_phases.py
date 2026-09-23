"""Placement contracts for JEV decisions; these describe dependencies, not beliefs."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Placement:
    purpose: str
    needs: tuple[str, ...]
    parallel_unit: str
    barrier: str


PLACEMENTS = {
    "rule_scope": Placement(
        "Bind each aggregate to the entity declared by the rule.",
        ("compiled rule domains", "catalog keys and relationships", "request"),
        "independent entity/relationship candidates",
        "Reject incompatible bindings before aggregating separate child branches.",
    ),
    "window_scope": Placement(
        "Bind each statistic's measure, partition and comparison population.",
        ("output operation", "typed measures", "resolved restrictions"),
        "statistic and restriction pair",
        "Compute inputs first; batch equal-population windows and branch unequal populations.",
    ),
    "latest": Placement(
        "Select observation identity and chronology before a parent join.",
        ("request", "observation fields", "entity references", "primary key"),
        "entity key and ordering decisions",
        "Reduce observations to one row per entity before calculating parent-level values.",
    ),
    "schema": Placement(
        "Find operands without fitting the whole schema into one request.",
        ("request", "catalog"),
        "catalog page",
        "Reconcile page winners and preserve relationship bridges.",
    ),
    "intent": Placement(
        "Identify output grain and calculation before binding its operands.",
        ("request", "focused catalog"),
        "independent objective factor",
        "Operands must be compatible with the selected operation.",
    ),
    "fields": Placement(
        "Bind output, grouping, filter and transformation roles independently.",
        ("request", "operation contracts", "field descriptions"),
        "field or role page",
        "Reconcile grain and shared entity identity before compilation.",
    ),
    "values": Placement(
        "Map language to observed values and typed predicates, preserving uncertainty.",
        ("request", "selected field", "observed dictionary", "operator"),
        "field, dictionary page or independent cohort",
        "Resolve competing page matches before applying a predicate.",
    ),
    "outputs": Placement(
        "Assign requested answer slots jointly; ranking operands are not implicit outputs.",
        ("request", "available expressions", "output obligations"),
        "output slot",
        "Enforce coverage and uniqueness jointly before projecting.",
    ),
    "candidates": Placement(
        "Compare complete executable alternatives against independent obligations.",
        ("request", "typed candidates"),
        "candidate and quality dimension",
        "Join all scores before selecting one coherent proposal.",
    ),
    "audit": Placement(
        "Check the composed proposal after all dependencies exist.",
        ("request", "compiled SQL", "resolved contracts"),
        "independent correctness dimension",
        "Uncertainty holds a proposal; it never converts an unexecuted stage to false.",
    ),
}


def placement(stage):
    if "window" in stage:
        kind = "window_scope"
    elif "latest" in stage:
        kind = "latest"
    elif stage == "entity_scope_reconciliation" or stage == "independent_relationship_paths":
        kind = "rule_scope"
    elif any(
        word in stage for word in ("intent", "objective", "requirements", "independent_stage")
    ):
        kind = "intent"
    elif any(word in stage for word in ("predicate", "literal", "value", "scope_reconciliation")):
        kind = "values"
    elif any(word in stage for word in ("audit", "repair")):
        kind = "audit"
    elif "candidate" in stage:
        kind = "candidates"
    elif any(word in stage for word in ("output", "result_contract")):
        kind = "outputs"
    elif any(word in stage for word in ("schema", "source_section", "source_reconciliation")):
        kind = "schema"
    elif any(
        word in stage for word in ("intent", "objective", "requirements", "independent_stage")
    ):
        kind = "intent"
    else:
        kind = "fields"
    return {"kind": kind, **asdict(PLACEMENTS[kind])}
