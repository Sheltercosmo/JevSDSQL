"""Fuse shared contexts; execute independent batches under one reserved job budget."""

import os
import random
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock

from ..evaluators import ProviderError
from ..generic.jev import validate_response
from ..ledger import digest
from . import schema
from .budget import Budget, estimate, token_bound
from .types import Decision, Policy, coverage
from .types import OperationState as Op

_cache_locks = [Lock() for _ in range(256)]


class AccountGate:
    def __init__(self):
        self.lock = Lock()
        self.requests, self.tokens = deque(), deque()

    def wait(self, tokens, cancelled):
        rpm = int(os.getenv("SDD_OPERATOR_RPM", "840"))
        tps = int(os.getenv("SDD_OPERATOR_TPS", "175000"))
        if rpm < 1 or tps < tokens:
            return False
        deadline = time.monotonic() + 60
        while not cancelled() and time.monotonic() < deadline:
            current = time.monotonic()
            with self.lock:
                while self.requests and self.requests[0] <= current - 60:
                    self.requests.popleft()
                while self.tokens and self.tokens[0][0] <= current - 1:
                    self.tokens.popleft()
                if len(self.requests) < rpm and sum(v for _, v in self.tokens) + tokens <= tps:
                    self.requests.append(current)
                    self.tokens.append((current, tokens))
                    return True
            time.sleep(0.05)
        return False


_gate = AccountGate()


def validate_question(question):
    if not isinstance(question, dict) or question.get("type") not in {"noul", "choice", "score"}:
        raise ValueError(
            "A declared noul/choice/score output type is required; prose/SQL generation is unsupported"
        )
    if not question.get("instructions"):
        raise ValueError("Question instructions are required")
    criteria = question.get("criteria")
    if question["type"] == "choice":
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
            raise ValueError("Choice requires 2–255 options including sentinels")
        if any(not isinstance(key, str) or not key or not value for key, value in criteria.items()):
            raise ValueError("Every Choice option requires an ID and meaningful description")
    if question["type"] == "score" and (
        not isinstance(criteria, list)
        or not 2 <= len(criteria) <= 10
        or any(not v for v in criteria)
    ):
        raise ValueError("Score requires 2–10 descriptive ordered levels")


class Runtime:
    def __init__(
        self, store, decisions, run_id, limits=None, policy=None, approved=False, source_check=None
    ):
        self.store, self.decisions, self.run_id = store, decisions, run_id
        self.budget, self.policy = Budget(limits), policy or Policy()
        self.approved, self.source_check = approved, source_check or (lambda connection=None: True)
        self.model = decisions.model if decisions else "unconfigured"
        self.warnings = []
        self.stages = 0

    def key(self, item):
        return digest(
            [
                "jev-operators-v1",
                self.store.tenant,
                self.model,
                item.subject_id,
                item.source_revisions,
                item.state,
                item.question,
                item.hypothesis,
            ]
        )

    def cached(self, item):
        row = self.store.cached(self.key(item))
        if row:
            return self.policy.resolve(
                row["answer"],
                observation_id=row["id"],
                cached=True,
                dependencies=item.source_revisions,
            )
        return None

    def cancelled(self):
        return self.store.cancelled(self.run_id)

    def evaluate(self, items):
        items = list(items)
        if len({item.id for item in items}) != len(items):
            raise ValueError("Work item IDs must be unique within a stage")
        for item in items:
            validate_question(item.question)
        if self.cancelled():
            return {i.id: Decision.unexecuted("Job cancelled", Op.CANCELLED) for i in items}
        self.stages += bool(items)
        if not self.source_check():
            return {
                i.id: Decision.unexecuted("Source changed or was deleted", Op.STALE) for i in items
            }
        results, missing, aliases = self._cached_items(items)
        batches = self._pack_batches(missing.values(), aliases, results)
        total_tokens = sum(self.payload_tokens(batch) for batch in batches)
        preview = estimate(
            sum(map(len, batches)), len(batches), total_tokens, self.budget.limits, self.stages
        )
        self.warnings.extend(preview["warnings"])
        blocked = "W03" in preview["warnings"] or (
            "W02" in preview["warnings"] and not self.approved
        )
        if blocked:
            for batch in batches:
                for item in batch:
                    for identity in aliases[self.key(item)]:
                        results[identity] = Decision.unexecuted(
                            "Work envelope requires approval or exceeds hard policy",
                            Op.BLOCKED_BY_BUDGET,
                        )
        else:
            self._execute_batches(batches, aliases, results)
        return {item.id: results[item.id] for item in items}

    def _cached_items(self, items):
        results, missing, aliases = {}, {}, defaultdict(list)
        for item in items:
            cached = self.cached(item)
            if cached is not None:
                results[item.id] = cached
            else:
                key = self.key(item)
                missing.setdefault(key, item)
                aliases[key].append(item.id)
        return results, missing, aliases

    def _pack_batches(self, items, aliases, results):
        groups = defaultdict(list)
        for item in items:
            groups[digest([item.state, item.source_revisions, item.hypothesis])].append(item)
        batches = []
        for group in groups.values():
            current = []
            for item in group:
                candidate = [*current, item]
                questions = {"q" + str(i): w.question for i, w in enumerate(candidate)}
                one = token_bound({"state": self.state(item), "question": item.question})
                total = token_bound(
                    {"model": self.model, "state": self.state(item), "questions": questions}
                )
                if one > 32000:
                    for identity in aliases[self.key(item)]:
                        results[identity] = Decision.unexecuted(
                            "W01: source plus question exceeds conservative context bound",
                            Op.BLOCKED_BY_POLICY,
                        )
                    continue
                if current and (len(candidate) > self.budget.limits.batch_size or total > 64000):
                    batches.append(current)
                    current = []
                current.append(item)
            if current:
                batches.append(current)
        return batches

    def _execute_batches(self, batches, aliases, results):
        iterator = iter(batches)
        with ThreadPoolExecutor(max_workers=self.budget.limits.concurrency) as pool:
            pending = {}

            def admit():
                batch = next(iterator, None)
                if batch is not None:
                    pending[pool.submit(self.batch, batch)] = batch
                    return True
                return False

            for _ in range(self.budget.limits.concurrency):
                admit()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    batch = pending.pop(future)
                    for item, decision in zip(batch, future.result()):
                        for identity in aliases[self.key(item)]:
                            results[identity] = decision
                    admit()

    @staticmethod
    def state(item):
        return (
            {"subject": item.state, "conditional_hypothesis": item.hypothesis}
            if item.hypothesis
            else item.state
        )

    def payload_tokens(self, batch):
        return token_bound(
            {
                "model": self.model,
                "state": self.state(batch[0]),
                "questions": {"q" + str(i): w.question for i, w in enumerate(batch)},
            }
        )

    def batch(self, batch):
        # Bound the lock pool while coalescing identical in-process batches.
        stripe = int(digest([self.store.tenant, [self.key(w) for w in batch]])[:8], 16) % len(
            _cache_locks
        )
        with _cache_locks[stripe]:
            cached = [self.cached(item) for item in batch]
            pending = [item for item, found in zip(batch, cached) if found is None]
            if not pending:
                return cached
            completed = self.dispatch(pending)
            selected = iter(completed)
            return [found if found is not None else next(selected) for found in cached]

    def dispatch(self, batch):
        tokens = self.payload_tokens(batch)
        questions = {"q" + str(i): item.question for i, item in enumerate(batch)}
        for attempt in range(self.budget.limits.retries + 1):
            if self.cancelled():
                return [Decision.unexecuted("Job cancelled", Op.CANCELLED) for _ in batch]
            if not self.source_check():
                return [Decision.unexecuted("Source is stale", Op.STALE) for _ in batch]
            if self.decisions is None:
                return [
                    Decision.unexecuted("JEV provider is not configured", Op.FAILED) for _ in batch
                ]
            if not self.budget.reserve(len(batch), tokens):
                return [
                    Decision.unexecuted("Reserved job budget exhausted", Op.BLOCKED_BY_BUDGET)
                    for _ in batch
                ]
            if not _gate.wait(tokens, self.cancelled):
                return [
                    Decision.unexecuted("Account throttle or cancellation", Op.BLOCKED_BY_BUDGET)
                    for _ in batch
                ]
            started = time.monotonic()
            try:
                reply = self.decisions.ask(self.store.tenant, self.state(batch[0]), questions)
                validate_response(reply, questions, self.model)
                self.budget.usage(reply.get("usage", {}))
                self.attempt(
                    batch, attempt, tokens, started, "SUCCEEDED", usage=reply.get("usage", {})
                )
                output = []
                with self.store.db.transaction(self.store.tenant) as connection:
                    if self.store.cancelled(self.run_id, connection) or not self.source_check(
                        connection
                    ):
                        return [
                            Decision.unexecuted(
                                "Source changed or job cancelled before commit", Op.STALE
                            )
                            for _ in batch
                        ]
                    for index, item in enumerate(batch):
                        answer = reply["answers"]["q" + str(index)]
                        row = self.store.add(
                            schema.observations,
                            connection=connection,
                            cache_key=self.key(item),
                            model=self.model,
                            subject_id=item.subject_id,
                            source_revisions=list(item.source_revisions),
                            question=item.question,
                            context_hash=digest(item.state),
                            hypothesis=item.hypothesis,
                            answer=answer,
                        )
                        output.append(
                            self.policy.resolve(
                                answer, observation_id=row["id"], dependencies=item.source_revisions
                            )
                        )
                return output
            except ProviderError as exc:
                self.attempt(batch, attempt, tokens, started, "FAILED", error=exc.code)
                if exc.code == "DailyBudgetExceeded":
                    return [Decision.unexecuted(exc.code, Op.BLOCKED_BY_BUDGET) for _ in batch]
                if not exc.retryable or attempt == self.budget.limits.retries:
                    return [Decision.unexecuted(exc.code, Op.FAILED) for _ in batch]
                delay = max(exc.retry_after or 0, 0.1 * 2**attempt + random.random() * 0.1)
                if delay > 60:
                    return [
                        Decision.unexecuted(
                            "Provider retry delay exceeds this run's 60-second retry window",
                            Op.BLOCKED_BY_BUDGET,
                        )
                        for _ in batch
                    ]
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if self.cancelled():
                        return [
                            Decision.unexecuted("Job cancelled during retry wait", Op.CANCELLED)
                            for _ in batch
                        ]
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise AssertionError("Retry loop must produce explicit outcomes")

    def attempt(self, batch, attempt, tokens, started, state, error=None, usage=None):
        self.store.add(
            schema.attempts,
            run_id=self.run_id,
            work_ids=[w.id for w in batch],
            attempt=attempt + 1,
            state=state,
            error=error,
            usage=usage or {},
            reserved_tokens=tokens,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )

    def manifest(self, decisions):
        return {
            **coverage(decisions),
            **self.budget.manifest(),
            "model": self.model,
            "policy_revision": self.policy.revision,
            "logical_evaluation_stages": self.stages,
            "warnings": sorted(set(self.warnings)),
        }
