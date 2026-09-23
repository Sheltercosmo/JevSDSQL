import time
from collections import Counter
from sqlalchemy import select, insert, update
from . import schema as s
from .ledger import Ledger, uid, now, digest
from .workers import Workers
from .compiler import population, final_relation, aggregate
from .ir import Plan


class Executor:
    def __init__(self, db, backends):
        self.db, self.ledger, self.workers = db, Ledger(db), Workers(db, backends)

    def execute(self, tenant, plan, inline=True):
        plan = Plan.model_validate(plan.model_dump() if isinstance(plan, Plan) else plan)
        started, stamp, run_id = time.monotonic(), now(), uid()
        concepts = {
            cid: self.ledger.get(tenant, s.concepts, cid)
            for cid in sorted(plan.predicate.concepts())
        }
        evaluator = self.ledger.get(tenant, s.evaluators, plan.evaluator_id)
        policy = self.ledger.get(tenant, s.policies, plan.policy_id)
        if any(c["status"] == "deprecated" for c in concepts.values()):
            raise ValueError("Deprecated concepts cannot create new runs")
        jobs, missing_context, reused, scheduled, assertions = {}, set(), 0, 0, {}
        with self.db.transaction(tenant) as cx:
            rows = [dict(r) for r in cx.execute(population(plan, tenant)).mappings()]
            eligible = len(rows)
            if plan.mode == "explore":
                rows = rows[: plan.candidate_limit]
            snapshot = [r["id"] for r in rows]
            # Pin reviewer decisions at query start rather than reading mutable decisions at finish.
            review_rows = cx.execute(
                select(s.assertions)
                .where(
                    s.assertions.c.tenant == tenant,
                    s.assertions.c.version_id.in_(snapshot),
                    s.assertions.c.created_at <= stamp,
                )
                .order_by(s.assertions.c.created_at, s.assertions.c.id)
            ).mappings()
            for a in review_rows:
                assertions[(a["version_id"], a["concept_id"])] = dict(a)
            cx.execute(
                insert(s.runs).values(
                    id=run_id,
                    tenant=tenant,
                    plan=plan.model_dump(),
                    snapshot=snapshot,
                    manifest={"status": "running"},
                    result={},
                    created_at=stamp,
                )
            )
            for version in rows:
                for cid, concept in concepts.items():
                    pair = (version["id"], cid)
                    if pair in assertions:
                        continue
                    if set(concept["context_fields"]) - version["context"].keys():
                        missing_context.add(pair)
                        continue
                    context_hash = digest(
                        {k: version["context"][k] for k in concept["context_fields"]}
                    )
                    jid = digest([tenant, version["id"], context_hash, cid, evaluator["id"]])
                    obs = cx.execute(
                        select(s.observations.c.id).where(
                            s.observations.c.tenant == tenant, s.observations.c.job_id == jid
                        )
                    ).first()
                    if obs:
                        jobs[pair] = jid
                        reused += 1
                    elif scheduled < plan.max_evaluations:
                        jobs[pair] = self.workers.enqueue(
                            tenant, version, concept, evaluator["id"], cx
                        )
                        scheduled += 1
        deadline = time.monotonic() + plan.wait_seconds
        if inline:
            while time.monotonic() < deadline and self.workers.work_one(
                tenant, list(jobs.values())
            ):
                pass
        with self.db.transaction(tenant) as cx:
            # Lock the run against deletion redaction before publishing any result.
            run = (
                cx.execute(
                    select(s.runs)
                    .where(s.runs.c.id == run_id, s.runs.c.tenant == tenant)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if run["manifest"].get("status") == "redacted_source_deleted":
                raise ValueError("Source deleted during execution; query was redacted")
            decisions = {v["id"]: {} for v in rows}
            details, reasons, observation_ids = [], Counter(), []
            for version in rows:
                for cid in concepts:
                    pair, decision, obs, reason, aid = (
                        (version["id"], cid),
                        None,
                        None,
                        "missing",
                        None,
                    )
                    if pair in assertions:
                        assertion = assertions[pair]
                        decision = {"true": True, "false": False, "unknown": None}[
                            assertion["decision"]
                        ]
                        reason, aid = "human_review", assertion["id"]
                    elif pair in missing_context:
                        reason = "missing_context"
                    elif pair in jobs:
                        obs = (
                            cx.execute(
                                select(s.observations).where(
                                    s.observations.c.tenant == tenant,
                                    s.observations.c.job_id == jobs[pair],
                                )
                            )
                            .mappings()
                            .first()
                        )
                        if obs:
                            p = obs["probability"]
                            decision = (
                                True
                                if p >= policy["accept"]
                                else False
                                if p <= policy["reject"]
                                else None
                            )
                            reason = (
                                "positive"
                                if decision is True
                                else "negative"
                                if decision is False
                                else "ambiguous"
                            )
                            observation_ids.append(obs["id"])
                        else:
                            job = self.ledger.get(tenant, s.jobs, jobs[pair], cx)
                            reason = (
                                "failed"
                                if job["error"]
                                else "pending"
                                if job["state"] != "cancelled"
                                else "cancelled"
                            )
                    else:
                        reason = "budget_exhausted"
                    decisions[version["id"]][cid] = decision
                    reasons[reason] += 1
                    details.append(
                        dict(
                            version_id=version["id"],
                            concept_id=cid,
                            decision=decision,
                            reason=reason,
                            observation_id=obs["id"] if obs else None,
                            assertion_id=aid,
                            probability=obs["probability"] if obs else None,
                        )
                    )
            relation = final_relation(plan, tenant, snapshot, decisions)
            statuses = list(cx.execute(select(relation)).mappings())
            counts = Counter(
                "positive"
                if r["matches"] is True
                else "negative"
                if r["matches"] is False
                else "unknown"
                for r in statuses
            )
            result = [dict(r) for r in cx.execute(aggregate(plan, relation)).mappings()]
            # For customer EXISTS, one positive message decides the customer despite other unknown messages.
            positive_entities = {
                r["customer_id"] if plan.grain == "customer" else r["id"]
                for r in statuses
                if r["matches"] is True
            }
            uncertain_entities = {
                r["customer_id"] if plan.grain == "customer" else r["id"]
                for r in statuses
                if r["matches"] is None
            } - positive_entities
            complete = plan.mode == "complete" and not uncertain_entities
            if plan.operation in ("group", "rank"):
                # An unresolved message can introduce membership in another group.
                complete = plan.mode == "complete" and not counts["unknown"]
            bounds = None
            if plan.operation == "count" and plan.mode == "complete":
                lower = result[0]["count"]
                bounds = {"confirmed": lower, "possible_maximum": lower + len(uncertain_entities)}
            manifest = dict(
                source_snapshot=digest(snapshot),
                source_version_ids=snapshot,
                population={
                    "scope": [p.model_dump() for p in plan.scope],
                    "start": plan.start,
                    "end_exclusive": plan.end,
                    "segment_time_semantics": "at_message_ingestion",
                },
                concept_revisions=list(concepts),
                evaluator_revision=evaluator["id"],
                policy_revision=policy["id"],
                observation_ids=observation_ids,
                assertion_ids=[a["id"] for a in assertions.values()],
                eligible_subjects=eligible,
                selected_subjects=len(rows),
                resolved_positive=counts["positive"],
                resolved_negative=counts["negative"],
                unresolved_subjects=counts["unknown"],
                unresolved_entities=len(uncertain_entities),
                observation_states=dict(reasons),
                reused_observations=reused,
                scheduled_pairs=scheduled,
                execution_mode=plan.mode,
                retrieval_method="source_version_order"
                if plan.mode == "explore"
                else "full_population",
                fully_evaluated=len(observation_ids) + sum(1 for d in details if d["assertion_id"])
                == len(rows) * len(concepts),
                count_bounds=bounds,
                complete=complete,
                status="complete"
                if complete
                else "incomplete"
                if plan.mode == "complete"
                else "exploratory",
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                quality="Classifier judgments under the pinned policy; semantic accuracy is not guaranteed.",
            )
            response = dict(run_id=run_id, result=result, manifest=manifest, evidence=details)
            cx.execute(
                update(s.runs)
                .where(s.runs.c.id == run_id, s.runs.c.tenant == tenant)
                .values(manifest=manifest, result={"rows": result, "evidence": details})
            )
            return response
