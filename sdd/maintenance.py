from sqlalchemy import insert, update
from . import schema as s
from .ledger import uid
from .ir import Plan, Predicate


def create_policy(
    ledger,
    tenant,
    concept_id,
    evaluator_id,
    policy_id,
    owner,
    population=None,
    budget=100,
    freshness_seconds=3600,
    required_coverage=1.0,
):
    concept = ledger.get(tenant, s.concepts, concept_id)
    ledger.get(tenant, s.evaluators, evaluator_id)
    ledger.get(tenant, s.policies, policy_id)
    if concept["status"] != "active":
        raise ValueError("Only active concepts may be maintained")
    if not owner or budget < 0 or freshness_seconds < 1 or not 0 <= required_coverage <= 1:
        raise ValueError("Invalid materialization policy")
    population = population or {}
    if set(population) - {"start", "end"}:
        raise ValueError("Population supports start/end only")
    Plan(
        predicate=Predicate(op="semantic", concept_id=concept_id),
        evaluator_id=evaluator_id,
        policy_id=policy_id,
        max_evaluations=budget,
        **population,
    )
    row = dict(
        id=uid(),
        tenant=tenant,
        concept_id=concept_id,
        evaluator_id=evaluator_id,
        policy_id=policy_id,
        owner=owner,
        population=population,
        budget=budget,
        freshness_seconds=freshness_seconds,
        required_coverage=required_coverage,
        last_run=None,
    )
    with ledger.db.transaction(tenant) as cx:
        cx.execute(insert(s.materializations).values(**row))
    return row


def refresh(executor, tenant, identity):
    m = executor.ledger.get(tenant, s.materializations, identity)
    concept = executor.ledger.get(tenant, s.concepts, m["concept_id"])
    if concept["status"] != "active":
        raise ValueError("Maintenance stopped: concept is not active")
    plan = Plan(
        operation="count",
        grain="message",
        predicate=Predicate(op="semantic", concept_id=m["concept_id"]),
        evaluator_id=m["evaluator_id"],
        policy_id=m["policy_id"],
        max_evaluations=m["budget"],
        **m["population"],
    )
    result = executor.execute(tenant, plan)
    coverage = result["manifest"]
    ratio = 1 - coverage["unresolved_subjects"] / max(1, coverage["eligible_subjects"])
    result["materialization"] = {"coverage": ratio, "target_met": ratio >= m["required_coverage"]}
    # Only publish a new generation when its declared coverage target is met.
    if result["materialization"]["target_met"]:
        with executor.db.transaction(tenant) as cx:
            cx.execute(
                update(s.materializations)
                .where(s.materializations.c.id == identity, s.materializations.c.tenant == tenant)
                .values(last_run=result["run_id"])
            )
    return result
