"""Batched semantic evaluation with short transactions and bounded record concurrency."""

import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from sqlalchemy import select, update, insert, delete

from ..ledger import uid, now, digest
from ..evaluators import ProviderError
from . import schema
from .catalog import Catalog, serial
from .jev import validate_response
from .semantic_types import PROTOCOL, SemanticSpec


class Semantics:
    def __init__(self, db, decisions, *, concurrency=None, batch=True):
        self.db, self.decisions = db, decisions
        self.concurrency = max(
            1, min(16, concurrency or int(os.getenv("SDD_JEV_CONCURRENCY", "4")))
        )
        self.batch = batch

    def source_matches(self, connection, dataset, row, table=None):
        table = table if table is not None else Catalog(self.db).table(dataset, connection)
        query = select(table).where(*(table.c[key] == row[key] for key in dataset["primary_key"]))
        if self.db.engine.dialect.name == "postgresql":
            query = query.with_for_update(read=True)
        current = connection.execute(query).mappings().first()
        return current is not None and digest(serial(dict(current))) == digest(serial(row))

    def _insert(self, table):
        return __import__(
            "sqlalchemy.dialects." + self.db.engine.dialect.name, fromlist=["insert"]
        ).insert(table)

    def _claim(self, tenant, dataset, table, row, specs, budget, values, stats):
        row_key = digest(serial([row[key] for key in dataset["primary_key"]]))
        version_id = digest([tenant, dataset["id"], row_key, digest(serial(row))])
        claims = []
        with self.db.transaction(tenant) as connection:
            if not self.source_matches(connection, dataset, row, table):
                return claims
            for spec in specs:
                counters = stats[spec.key]
                if row[spec.column] is None:
                    counters["missing_subject"] += 1
                    continue
                context = spec.context(row)
                dependency_hash = digest(context)
                identity = digest(
                    [
                        tenant,
                        dataset["id"],
                        row_key,
                        dependency_hash,
                        spec.key,
                        self.decisions.model,
                    ]
                )
                if spec.feature_id:
                    review = (
                        connection.execute(
                            select(schema.feature_reviews)
                            .where(
                                schema.feature_reviews.c.tenant == tenant,
                                schema.feature_reviews.c.feature_id == spec.feature_id,
                                schema.feature_reviews.c.row_key == row_key,
                                schema.feature_reviews.c.dependency_hash == dependency_hash,
                            )
                            .order_by(
                                schema.feature_reviews.c.created_at.desc(),
                                schema.feature_reviews.c.id.desc(),
                            )
                            .limit(1)
                        )
                        .mappings()
                        .first()
                    )
                    if review:
                        values[spec.key][row_key] = review["value"]
                        counters["reviewed"] += 1
                        counters["observations"].append(
                            {
                                "review_id": review["id"],
                                "source_version_id": review["source_version_id"],
                                "row_key": row_key,
                                "decision": review["value"],
                                "source": "human",
                            }
                        )
                        continue
                observation = (
                    connection.execute(
                        select(schema.evidence).where(
                            schema.evidence.c.id == identity, schema.evidence.c.tenant == tenant
                        )
                    )
                    .mappings()
                    .first()
                )
                if observation and observation["state"] == "succeeded":
                    payload = (
                        connection.execute(
                            select(schema.payloads).where(
                                schema.payloads.c.evidence_id == identity,
                                schema.payloads.c.tenant == tenant,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if payload:
                        saved = payload["answer"]
                        value, probability, span = spec.resolve(
                            saved["primary"],
                            saved["answers"],
                            saved["question_id"],
                            saved["spans"],
                            counters["accept"],
                            counters["reject"],
                        )
                        values[spec.key][row_key] = value
                        counters["reused"] += 1
                        counters["observations"].append(
                            {
                                "id": identity,
                                "source_version_id": payload["source_version_id"],
                                "row_key": row_key,
                                "probability": probability,
                                "decision": value,
                                "span": span,
                            }
                        )
                        continue
                if (
                    observation
                    and observation["state"] in ("pending", "running")
                    and observation["attempts"] >= 3
                    and observation["lease_until"] < time.time()
                ):
                    connection.execute(
                        update(schema.evidence)
                        .where(
                            schema.evidence.c.id == identity,
                            schema.evidence.c.tenant == tenant,
                            schema.evidence.c.state.in_(["pending", "running"]),
                            schema.evidence.c.lease_until < time.time(),
                            schema.evidence.c.attempts >= 3,
                        )
                        .values(
                            state="failed", error="LeaseExpiredAfterRetryLimit", lease_token=None
                        )
                    )
                    connection.execute(
                        update(schema.attempts)
                        .where(
                            schema.attempts.c.evidence_id == identity,
                            schema.attempts.c.tenant == tenant,
                            schema.attempts.c.state == "running",
                        )
                        .values(state="failed", error="LeaseExpiredAfterRetryLimit")
                    )
                    counters["failed"] += 1
                    continue
                if observation and observation["state"] == "failed":
                    counters["failed"] += 1
                    continue
                if budget[0] <= 0:
                    counters["budget_exhausted"] += 1
                    continue
                question_id = "predicate" if len(specs) == 1 else "q" + str(len(claims))
                questions, spans = spec.questions(row, question_id)
                if not questions:
                    counters["no_candidates"] += 1
                    continue
                connection.execute(
                    self._insert(schema.row_versions)
                    .values(
                        id=version_id,
                        tenant=tenant,
                        dataset_id=dataset["id"],
                        row_key=row_key,
                        row_hash=digest(serial(row)),
                        value=serial(row),
                        created_at=now(),
                    )
                    .on_conflict_do_nothing()
                )
                connection.execute(
                    self._insert(schema.evidence)
                    .values(
                        id=identity,
                        tenant=tenant,
                        dataset_id=dataset["id"],
                        row_key=row_key,
                        row_hash=dependency_hash,
                        definition=spec.definition,
                        evaluator=self.decisions.model,
                        state="pending",
                        usage={},
                        lease_until=0,
                        attempts=0,
                        created_at=now(),
                    )
                    .on_conflict_do_nothing()
                )
                token = uid()
                attempt = connection.execute(
                    update(schema.evidence)
                    .where(
                        schema.evidence.c.id == identity,
                        schema.evidence.c.tenant == tenant,
                        schema.evidence.c.state.in_(["pending", "running"]),
                        schema.evidence.c.lease_until < time.time(),
                        schema.evidence.c.attempts < 3,
                    )
                    .values(
                        state="running",
                        lease_token=token,
                        lease_until=time.time() + 120,
                        attempts=schema.evidence.c.attempts + 1,
                    )
                    .returning(schema.evidence.c.attempts)
                ).scalar_one_or_none()
                if attempt is None:
                    counters["pending"] += 1
                    continue
                connection.execute(
                    update(schema.attempts)
                    .where(
                        schema.attempts.c.evidence_id == identity,
                        schema.attempts.c.tenant == tenant,
                        schema.attempts.c.state == "running",
                    )
                    .values(state="superseded", error="LeaseExpired")
                )
                attempt_id = uid()
                connection.execute(
                    insert(schema.attempts).values(
                        id=attempt_id,
                        tenant=tenant,
                        evidence_id=identity,
                        state="running",
                        usage={},
                        created_at=now(),
                    )
                )
                budget[0] -= 1
                claims.append(
                    {
                        "id": identity,
                        "spec": spec,
                        "token": token,
                        "attempt": attempt,
                        "attempt_id": attempt_id,
                        "row_key": row_key,
                        "version_id": version_id,
                        "questions": questions,
                        "question_id": question_id,
                        "spans": spans,
                    }
                )
        return claims

    def _infer(self, tenant, row, claims):
        started = time.perf_counter()
        questions = {key: value for claim in claims for key, value in claim["questions"].items()}
        spec = claims[0]["spec"]
        state = {
            "subject": serial(row[spec.column]),
            "context": spec.context(row),
            "subject_column": spec.column,
        }
        try:
            response = self.decisions.ask(tenant, state, questions)
            validate_response(response, questions, self.decisions.model)
            return response, None, False, (time.perf_counter() - started) * 1000
        except Exception as exc:
            error = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
            return (
                None,
                error,
                isinstance(exc, ProviderError) and exc.retryable,
                (time.perf_counter() - started) * 1000,
            )

    def _publish(self, tenant, dataset, table, row, claims, call_id, outcome, values, stats):
        response, error, retryable, elapsed = outcome
        usage = response.get("usage", {}) if response else {}
        owner_stats = stats[claims[0]["spec"].key]
        owner_stats["requests"] += 1
        owner_stats["questions"] += sum(len(claim["questions"]) for claim in claims)
        owner_stats["input_tokens"] += usage.get("input_tokens", 0)
        owner_stats["output_tokens"] += usage.get("output_tokens", 0)
        owner_stats["provider_ms"] += elapsed
        with self.db.transaction(tenant) as connection:
            alive = self.source_matches(connection, dataset, row, table)
            connection.execute(
                update(schema.inference_calls)
                .where(
                    schema.inference_calls.c.id == call_id,
                    schema.inference_calls.c.tenant == tenant,
                )
                .values(
                    state="failed" if error else "succeeded",
                    usage=usage,
                    elapsed_ms=elapsed,
                    error=error,
                )
            )
            for claim in claims:
                spec = claim["spec"]
                counters = stats[spec.key]
                counters["evaluated"] += 1
                current = (
                    connection.execute(
                        select(schema.evidence).where(
                            schema.evidence.c.id == claim["id"],
                            schema.evidence.c.tenant == tenant,
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not current
                    or current["state"] != "running"
                    or current["lease_token"] != claim["token"]
                ):
                    connection.execute(
                        update(schema.attempts)
                        .where(
                            schema.attempts.c.id == claim["attempt_id"],
                            schema.attempts.c.tenant == tenant,
                        )
                        .values(state="superseded")
                    )
                    continue
                if not alive:
                    connection.execute(
                        delete(schema.evidence).where(
                            schema.evidence.c.id == claim["id"],
                            schema.evidence.c.tenant == tenant,
                            schema.evidence.c.lease_token == claim["token"],
                        )
                    )
                    counters["failed"] += 1
                    continue
                value = probability = span = None
                if response:
                    answer = response["answers"][claim["question_id"]]
                    value, probability, span = spec.resolve(
                        answer,
                        response["answers"],
                        claim["question_id"],
                        claim["spans"],
                        counters["accept"],
                        counters["reject"],
                    )
                state = (
                    ("pending" if retryable and claim["attempt"] < 3 else "failed")
                    if error
                    else "succeeded"
                )
                connection.execute(
                    update(schema.evidence)
                    .where(
                        schema.evidence.c.id == claim["id"],
                        schema.evidence.c.tenant == tenant,
                        schema.evidence.c.lease_token == claim["token"],
                        schema.evidence.c.state == "running",
                    )
                    .values(
                        state=state,
                        probability=probability,
                        responder=response["model"] if response else None,
                        usage={"call_id": call_id},
                        error=error,
                        lease_until=time.time() + 2 if error else 0,
                    )
                )
                connection.execute(
                    update(schema.attempts)
                    .where(
                        schema.attempts.c.id == claim["attempt_id"],
                        schema.attempts.c.tenant == tenant,
                    )
                    .values(state=state, error=error, usage={"call_id": call_id})
                )
                if error:
                    counters["failed"] += 1
                else:
                    saved = {
                        "primary": answer,
                        "answers": {key: response["answers"][key] for key in claim["questions"]},
                        "question_id": claim["question_id"],
                        "spans": claim["spans"],
                    }
                    connection.execute(
                        self._insert(schema.payloads)
                        .values(
                            id=claim["id"],
                            tenant=tenant,
                            evidence_id=claim["id"],
                            source_version_id=claim["version_id"],
                            feature_id=spec.feature_id,
                            answer=saved,
                            span=span,
                            created_at=now(),
                        )
                        .on_conflict_do_nothing()
                    )
                values[spec.key][claim["row_key"]] = value
                counters["observations"].append(
                    {
                        "id": claim["id"],
                        "source_version_id": claim["version_id"],
                        "row_key": claim["row_key"],
                        "probability": probability,
                        "decision": value,
                        "span": span,
                        "call_id": call_id,
                    }
                )

    def ensure_many(
        self,
        tenant,
        dataset,
        rows,
        specs,
        budget,
        accept=0.8,
        reject=0.2,
        cancel_event=None,
        should_continue=None,
    ):
        if not 0 <= reject < accept <= 1:
            raise ValueError("Invalid thresholds")
        specs = list({spec.key: spec for spec in specs}.values())
        if len(specs) > 32:
            raise ValueError("At most 32 semantic attributes per dataset per query")
        started = time.perf_counter()
        values, stats = {}, {}
        keys = [digest(serial([row[key] for key in dataset["primary_key"]])) for row in rows]
        for spec in specs:
            values[spec.key] = dict.fromkeys(keys)
            stats[spec.key] = {
                **dict.fromkeys(
                    (
                        "reused",
                        "evaluated",
                        "unknown",
                        "missing_subject",
                        "failed",
                        "budget_exhausted",
                        "reviewed",
                        "pending",
                        "no_candidates",
                        "requests",
                        "questions",
                        "input_tokens",
                        "output_tokens",
                    ),
                    0,
                ),
                "provider_ms": 0.0,
                "observations": [],
                "protocol": PROTOCOL,
                "accept": accept,
                "reject": reject,
            }
        with self.db.transaction(tenant) as connection:
            table = Catalog(self.db).table(dataset, connection)
        groups = defaultdict(list)
        for spec in specs:
            group_key = (spec.column, spec.context_columns, None if self.batch else spec.key)
            groups[group_key].append(spec)
        pending = {}
        peak = 0
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="sdd-jev") as pool:

            def drain(all_pending=False):
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    row, claims, call_id = pending.pop(future)
                    self._publish(
                        tenant, dataset, table, row, claims, call_id, future.result(), values, stats
                    )
                if all_pending and pending:
                    drain(True)

            for row in rows:
                if (cancel_event is not None and cancel_event.is_set()) or (
                    should_continue is not None and not should_continue()
                ):
                    break
                for group in groups.values():
                    if len(pending) >= self.concurrency:
                        drain()
                    claims = self._claim(tenant, dataset, table, row, group, budget, values, stats)
                    if not claims:
                        continue
                    call_id = uid()
                    with self.db.transaction(tenant) as connection:
                        connection.execute(
                            insert(schema.inference_calls).values(
                                id=call_id,
                                tenant=tenant,
                                dataset_id=dataset["id"],
                                row_key=claims[0]["row_key"],
                                model=self.decisions.model,
                                questions=sum(len(claim["questions"]) for claim in claims),
                                state="running",
                                usage={},
                                elapsed_ms=0,
                                created_at=now(),
                            )
                        )
                    pending[pool.submit(self._infer, tenant, row, claims)] = (row, claims, call_id)
                    peak = max(peak, len(pending))
            if pending:
                drain(True)
        totals = Counter()
        row_order = {key: index for index, key in enumerate(keys)}
        for spec in specs:
            stats[spec.key]["observations"].sort(key=lambda item: row_order[item["row_key"]])
            stats[spec.key]["unknown"] = sum(value is None for value in values[spec.key].values())
            totals.update(
                {key: value for key, value in stats[spec.key].items() if isinstance(value, int)}
            )
        totals["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        totals["concurrency"] = self.concurrency
        totals["peak_pending_requests"] = peak
        totals["eligible_rows"] = len(rows)
        return values, stats, dict(totals)

    def ensure(self, tenant, dataset, rows, column, definition, budget, accept=0.8, reject=0.2):
        spec = SemanticSpec(column, definition)
        values, stats, _ = self.ensure_many(tenant, dataset, rows, [spec], budget, accept, reject)
        return values[spec.key], stats[spec.key]
