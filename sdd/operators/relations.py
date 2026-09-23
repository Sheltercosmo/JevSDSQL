"""Bounded pair relations, evidence coverage and population-aware statistics."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from itertools import combinations

from .arithmetic import aggregate as sql_aggregate
from .core import question
from .types import Decision, WorkItem, logical
from .types import OperationState as Op
from .types import OutputState as Out


class RelationalOperators:
    def candidate_pairs(self, left, right, candidate_policy=None):
        left, right = self.subjects(left), self.subjects(right)
        policy = candidate_policy or {"kind": "all_pairs"}
        kind = policy.get("kind", "all_pairs")
        if kind == "all_pairs":
            self.admit_expansion(len(left) * len(right))
            pairs = [(a, b) for a in left for b in right]
        elif kind == "exact_keys":
            keys = policy.get("keys")
            if not keys:
                raise ValueError("Exact blocking requires left/right key pairs")
            buckets = defaultdict(list)
            for b in right:
                key = tuple(self.field(b, k[1]) for k in keys)
                if None not in key:
                    buckets[key].append(b)
            pairs = []
            for a in left:
                candidates = buckets.get(tuple(self.field(a, k[0]) for k in keys), [])
                self.admit_expansion(len(pairs) + len(candidates))
                pairs.extend((a, b) for b in candidates)
        elif kind == "explicit":
            li, ri = {s["id"]: s for s in left}, {s["id"]: s for s in right}
            ids = policy.get("pairs", [])
            if len(set(tuple(p) for p in ids)) != len(ids):
                raise ValueError("Candidate pairs must be unique")
            if any(a not in li or b not in ri for a, b in ids):
                raise ValueError("Candidate pair is outside the supplied population")
            self.admit_expansion(len(ids))
            pairs = [(li[a], ri[b]) for a, b in ids]
            self.scope_incomplete |= len(pairs) < len(left) * len(right)
        else:
            raise ValueError("Candidate policy is all_pairs, exact_keys or explicit")
        return (
            left,
            right,
            pairs,
            {
                "left": len(left),
                "right": len(right),
                "pairs": len(pairs),
                "policy": policy,
                "all_pairs": kind == "all_pairs",
            },
        )

    def join(
        self, left, right, predicate, candidate_policy=None, mode="exhaustive", join_type="inner"
    ):
        """Join two populations using a semantic predicate."""
        if join_type not in {"inner", "left", "anti"}:
            raise ValueError("Semantic join supports inner, left and anti")
        left, right, pairs, scope = self.candidate_pairs(left, right, candidate_policy)
        definition = self.definition(predicate)
        items = []
        for i, (a, b) in enumerate(pairs):
            items.append(
                WorkItem(
                    str(i),
                    {"left": a["state"], "right": b["state"]},
                    question("noul", definition["instructions"]),
                    a["id"] + ":" + b["id"],
                    tuple(a["revisions"] + b["revisions"]),
                )
            )
        decisions = self.runtime.evaluate(items)
        edges = []
        unmatched = []
        unresolved = []
        by_left = defaultdict(list)
        for i, (a, b) in enumerate(pairs):
            d = decisions[str(i)]
            by_left[a["id"]].append(d)
            if d.output_state == Out.VALUE and d.value is True:
                edges.append({"left": a["id"], "right": b["id"], "decision": d.json()})
            elif d.output_state != Out.VALUE:
                unresolved.append({"left": a["id"], "right": b["id"], "decision": d.json()})
        for a in left:
            values = by_left[a["id"]]
            exhaustive = scope["all_pairs"] or scope["policy"].get("kind") == "exact_keys"
            if exhaustive and all(d.output_state == Out.VALUE and d.value is False for d in values):
                unmatched.append(a["id"])
        if join_type == "inner":
            rows = edges
        elif join_type == "left":
            rows = [*edges, *[{"left": identity, "right": None} for identity in unmatched]]
        else:
            rows = [{"left": identity} for identity in unmatched]
        return self.result(
            {
                "rows": rows,
                "unresolved_pairs": unresolved,
                "unmatched_left": unmatched,
                "scope": scope,
                "mode": mode,
            },
            decisions,
        )

    def align(self, left, right, candidate_policy, identity_definition):
        """Propose identity matches and check companion fields."""
        _, _, pairs, scope = self.candidate_pairs(left, right, candidate_policy)
        definition = self.definition(identity_definition)
        work, checks = [], {}
        for index, (a, b) in enumerate(pairs):
            fields = definition.get("fields")
            if fields is None:
                fields = (
                    [
                        {"left": name, "right": name}
                        for name in sorted(set(a["state"]) & set(b["state"]))
                    ]
                    if isinstance(a["state"], dict) and isinstance(b["state"], dict)
                    else []
                )
            if len(fields) > 32:
                raise ValueError("Alignment supports at most 32 companion field checks per pair")
            state = {"left": a["state"], "right": b["state"]}
            questions = {"identity": question("noul", definition["instructions"])}
            for i, field in enumerate(fields):
                questions["field:" + str(i)] = question(
                    "choice",
                    {
                        "question": "Compare these attributes in context; different spellings can express the same value, but missing data is insufficient.",
                        "left_field": field["left"],
                        "right_field": field["right"],
                    },
                    {
                        "consistent": "Attributes are consistent",
                        "different": "Attributes conflict",
                        "insufficient": "Missing or ambiguous attribute",
                    },
                )
            checks[index] = fields
            for name, q in questions.items():
                work.append(
                    WorkItem(
                        f"{index}:{name}",
                        state,
                        q,
                        a["id"] + ":" + b["id"],
                        tuple(a["revisions"] + b["revisions"]),
                    )
                )
        decisions = self.runtime.evaluate(work)
        return self.result(
            {
                "edges": [
                    {
                        "left": a["id"],
                        "right": b["id"],
                        "identity": decisions[f"{i}:identity"].json(),
                        "field_checks": [
                            {**field, "decision": decisions[f"{i}:field:{j}"].json()}
                            for j, field in enumerate(checks[i])
                        ],
                    }
                    for i, (a, b) in enumerate(pairs)
                ],
                "scope": scope,
                "authoritative_merge": False,
                "requires_merge_review": True,
            },
            decisions,
        )

    def verify(self, claim, evidence, scope=None):
        """Assess support and opposition for one claim."""
        sources = self.subjects(evidence)
        state = {"claim": claim, "evidence": [s["state"] for s in sources], "scope": scope}
        subject = {
            "id": "claim",
            "state": state,
            "revisions": [v for s in sources for v in s["revisions"]],
        }
        decisions = self.assess(
            [subject],
            {
                "support": question(
                    "noul",
                    "Does the supplied evidence explicitly support this exact claim, with the same entity, time and modality? Co-occurrence or a plan is not proof of completion.",
                ),
                "opposition": question(
                    "noul",
                    "Does the supplied evidence explicitly contradict this exact claim? Missing support alone is not contradiction.",
                ),
            },
        )
        stance = self.stance(decisions["claim:support"], decisions["claim:opposition"])
        return self.result(
            {
                "stance": stance.json(),
                "source_ids": [s["id"] for s in sources],
                "scope": scope,
                "real_world_truth_verified": False,
            },
            decisions,
        )

    @staticmethod
    def stance(support, opposition):
        if any(d.output_state == Out.NOT_EVALUATED for d in (support, opposition)):
            return Decision.unexecuted(
                "Support or opposition was not evaluated", Op.BLOCKED_BY_DEPENDENCY
            )
        if support.output_state != Out.VALUE or opposition.output_state != Out.VALUE:
            return Decision.unknown("Support or opposition remains unresolved")
        if support.value and opposition.value:
            return Decision.known("conflict")
        if support.value:
            return Decision.known("support")
        if opposition.value:
            return Decision.known("opposition")
        return Decision.known("insufficient")

    def relate(
        self,
        left,
        right,
        relation_rev,
        evidence_policy=None,
        mode="exhaustive",
        candidate_policy=None,
    ):
        """Assess a directed relationship between supplied records."""
        _, _, pairs, scope = self.candidate_pairs(left, right, candidate_policy)
        definition = self.definition(relation_rev)
        work = []
        for i, (a, b) in enumerate(pairs):
            state = {"left": a["state"], "right": b["state"]}
            for kind in ("support", "opposition"):
                q = question(
                    "noul",
                    {
                        "relation": definition["instructions"],
                        "direction": "left to right",
                        "judgment": kind,
                        "rules": "Use explicit textual evidence and correct attribution; absence of support is not opposition.",
                    },
                )
                work.append(
                    WorkItem(
                        f"{i}:{kind}",
                        state,
                        q,
                        a["id"] + ":" + b["id"],
                        tuple(a["revisions"] + b["revisions"]),
                    )
                )
        decisions = self.runtime.evaluate(work)
        edges = [
            {
                "left": a["id"],
                "right": b["id"],
                "stance": self.stance(
                    decisions[f"{i}:support"], decisions[f"{i}:opposition"]
                ).json(),
                "evidence": [
                    {
                        "subject_id": s["id"],
                        "start": 0,
                        "end": len(self.text(s)),
                        "text": self.text(s),
                    }
                    for s in (a, b)
                ],
            }
            for i, (a, b) in enumerate(pairs)
        ]
        return self.result(
            {
                "edges": edges,
                "scope": scope,
                "evidence_policy": evidence_policy,
                "mode": mode,
                "causal_fact": False,
            },
            decisions,
        )

    def cover(self, obligations, evidence_scope, relation_rev=None, mode="exhaustive"):
        """Check obligations against an evidence population."""
        obligations = self.subjects(obligations)
        sources = self.subjects(evidence_scope)
        _, _, pairs, scope = self.candidate_pairs(obligations, sources)
        work = []
        for i, (claim, evidence) in enumerate(pairs):
            for kind in ("support", "opposition"):
                q = question(
                    "noul",
                    {
                        "judgment": kind,
                        "rules": "Assess the obligation against only this supplied evidence. Plans are not completed actions; missing support is not opposition.",
                        "relation": self.definition(relation_rev) if relation_rev else None,
                    },
                )
                work.append(
                    WorkItem(
                        f"{i}:{kind}",
                        {"obligation": claim["state"], "evidence": evidence["state"]},
                        q,
                        claim["id"] + ":" + evidence["id"],
                        tuple(claim["revisions"] + evidence["revisions"]),
                    )
                )
        decisions = self.runtime.evaluate(work)
        output = []
        for claim in obligations:
            indices = [i for i, (a, _) in enumerate(pairs) if a["id"] == claim["id"]]
            supports = [decisions[f"{i}:support"] for i in indices]
            opposing = [decisions[f"{i}:opposition"] for i in indices]
            a = logical("or", supports) if supports else Decision.known(False)
            b = logical("or", opposing) if opposing else Decision.known(False)
            output.append(
                {
                    "obligation": claim["id"],
                    "stance": self.stance(a, b).json(),
                    "supporting_sources": [
                        pairs[i][1]["id"]
                        for i in indices
                        if decisions[f"{i}:support"].output_state == Out.VALUE
                        and decisions[f"{i}:support"].value is True
                    ],
                }
            )
        return self.result(
            {
                "coverage": output,
                "scope": scope,
                "absence_claim": "No support only within the declared evidence snapshot",
            },
            decisions,
        )

    def aggregate(
        self, relation, semantic_predicates, group_by=None, metric=None, mode="exhaustive"
    ):
        """Run SQL arithmetic over accepted semantic labels."""
        subjects = self.subjects(relation)
        predicates = [self.definition(p) for p in semantic_predicates]
        metric = metric or {"kind": "count"}
        kind = metric.get("kind", "count")
        if kind not in {"count", "sum", "avg", "min", "max"}:
            raise ValueError("Unsupported deterministic aggregate")
        decisions = self.assess(
            subjects,
            {
                str(i): question("noul", p["instructions"], p.get("criteria"))
                for i, p in enumerate(predicates)
            },
        )
        groups = defaultdict(list)
        for s in subjects:
            dims = tuple(self.field(s, k) for k in (group_by or []))
            d = (
                logical("and", [decisions[s["id"] + ":" + str(i)] for i in range(len(predicates))])
                if predicates
                else Decision.known(True)
            )
            groups[dims].append((s, d))
        if not subjects and not group_by:
            groups[()] = []
        output = []
        for dims, members in groups.items():
            included = [s for s, d in members if d.output_state == Out.VALUE and d.value is True]
            unknown = sum(d.output_state != Out.VALUE for _, d in members)
            values = (
                [self.field(s, metric.get("field")) for s in included] if kind != "count" else []
            )
            decimal_output = metric.get("value_type") == "decimal" or any(
                s.get("column_types", {}).get(metric.get("field")) == "number" for s in included
            )
            self._validate_aggregate_values(values, decimal_output)
            if metric.get("null_policy", "ignore") not in {"ignore", "error"}:
                raise ValueError("Null policy must be ignore or error")
            if metric.get("null_policy") == "error" and None in values:
                raise ValueError("Null aggregate operand")
            value = sql_aggregate(
                kind, values if kind != "count" else [1] * len(included), decimal_output
            )
            output.append(
                {
                    "group": dict(zip(group_by or [], dims)),
                    "value": value if not unknown else None,
                    "known_subset_value": value,
                    "eligible": len(members),
                    "included": len(included),
                    "unresolved": unknown,
                    "count_bounds": [len(included), len(included) + unknown]
                    if kind == "count"
                    else None,
                }
            )
        return self.result(
            {
                "groups": output,
                "metric": metric,
                "mode": mode,
                "arithmetic": "SQL aggregation over accepted labels; unresolved labels excluded only from known_subset_value",
            },
            decisions,
        )

    @staticmethod
    def _validate_aggregate_values(values, allow_decimal_strings):
        for value in values:
            if value is None:
                continue
            numeric = type(value) in (int, float)
            decimal_string = allow_decimal_strings and isinstance(value, str)
            if not numeric and not decimal_string:
                raise ValueError("Aggregate operands must be finite numeric values")
            try:
                if not Decimal(str(value)).is_finite():
                    raise ValueError("Aggregate operands must be finite numeric values")
            except InvalidOperation as exc:
                raise ValueError("Invalid decimal aggregate operand") from exc

    def contrast(self, left, right, concepts, unit, mode="exhaustive", discovery_split=None):
        """Compare concept rates across disjoint populations."""
        a, b = self.subjects(left), self.subjects(right)
        if not unit:
            raise ValueError("Contrast requires a declared population unit")
        keys_a = [self.field(s, unit) for s in a]
        keys_b = [self.field(s, unit) for s in b]
        if None in keys_a + keys_b or len(set(keys_a)) != len(a) or len(set(keys_b)) != len(b):
            raise ValueError(
                "Contrast population unit must be non-null and unique within each cohort"
            )
        if set(keys_a) & set(keys_b):
            raise ValueError("Contrast cohorts overlap at the declared unit")
        if discovery_split and set(discovery_split) & (set(keys_a) | set(keys_b)):
            raise ValueError("Discovery and confirmation populations overlap")
        renamed = [
            {**s, "id": side + ":" + s["id"]}
            for side, values in (("left", a), ("right", b))
            for s in values
        ]
        definitions = [self.definition(c) for c in concepts]
        decisions = self.assess(
            renamed,
            {str(i): question("noul", c["instructions"]) for i, c in enumerate(definitions)},
        )
        output = []
        for i in range(len(definitions)):
            rates = {}
            for side, population in (("left", a), ("right", b)):
                values = [decisions[side + ":" + s["id"] + ":" + str(i)] for s in population]
                positive = sum(d.output_state == Out.VALUE and d.value is True for d in values)
                unresolved = sum(d.output_state != Out.VALUE for d in values)
                n = len(population)
                rates[side] = {
                    "denominator": n,
                    "positive": positive,
                    "unresolved": unresolved,
                    "rate": positive / n if n and not unresolved else None,
                    "bounds": [positive / n, (positive + unresolved) / n] if n else None,
                }
            left_rate, right_rate = rates["left"]["rate"], rates["right"]["rate"]
            output.append(
                {
                    "concept": i,
                    **rates,
                    "difference": left_rate - right_rate
                    if left_rate is not None and right_rate is not None
                    else None,
                }
            )
        return self.result(
            {
                "contrasts": output,
                "unit": unit,
                "hypotheses": len(concepts),
                "causal": False,
                "inference": "Descriptive only; no p-values or population generalization claimed",
                "mode": mode,
            },
            decisions,
        )

    def evidence_join(self, claims, candidate_sources, max_bundle_size=2, scope=None):
        """Evaluate isolated evidence bundles for claims."""
        claims = self.subjects(claims)
        sources = self.subjects(candidate_sources)
        if len(sources) > 8 or not 1 <= max_bundle_size <= 2:
            raise ValueError(
                "Pilot evidence search supports at most 8 sources and singleton/pair bundles"
            )
        bundles = [
            bundle
            for size in range(1, max_bundle_size + 1)
            for bundle in combinations(sources, size)
        ]
        self.admit_expansion(len(claims) * len(bundles) * 2)
        work = []
        index = []
        for claim in claims:
            for bundle in bundles:
                identity = str(len(index))
                index.append((claim, bundle))
                for kind in ("support", "opposition"):
                    work.append(
                        WorkItem(
                            identity + ":" + kind,
                            {"claim": claim["state"], "evidence": [s["state"] for s in bundle]},
                            question(
                                "noul",
                                f"Does this exact evidence bundle jointly provide explicit {kind} for the claim? No evidence outside the bundle is available. Missing support is not contradiction.",
                            ),
                            claim["id"],
                            tuple(claim["revisions"] + [v for s in bundle for v in s["revisions"]]),
                        )
                    )
        decisions = self.runtime.evaluate(work)
        output = [
            {
                "claim": claim["id"],
                "sources": [s["id"] for s in bundle],
                "stance": self.stance(
                    decisions[str(i) + ":support"], decisions[str(i) + ":opposition"]
                ).json(),
            }
            for i, (claim, bundle) in enumerate(index)
        ]
        return self.result(
            {
                "bundles": output,
                "tested_subset_space": len(index),
                "maximum_bundle_size": max_bundle_size,
                "scope": scope,
                "globally_minimal_proof": False,
            },
            decisions,
        )
